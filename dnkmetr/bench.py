"""Слой управления стендом для интерактивного полуавтоматического режима (см. UI
в ui.py). Та же аппаратная логика, что в measure.run_session() (предполётная
проверка, бинарный поиск, стадии стабилизации, safety-abort'ы), но разложенная
на явные шаги под ручное управление оператором:

    "Включить уставку"  -> start(setpoint_a)   # бинарный поиск + стабилизация,
                                                #  дальше фоновый поток держит ток
    "Измерить" (много раз)  -> measure_async(n) # читает измерительный DMM,
                                                #  не мешая фоновому удержанию
    "Приостановить стенд"  -> pause()           # гасит выход, освобождает
                                                #  датчик для переподключения

Ничего не пишет на диск — оператор видит результат на экране (см. обсуждение
12.08.2026: сознательный отказ от CSV в пользу "промерили и посмотрели").
Приборы (VISA-хендлы) переиспользуются между циклами start()/pause() — заново
не открываются, пока не вызван close().
"""

import queue
import threading
import time

from accuracy import compute_relative_error
from measure import (
    build_controller,
    build_instruments,
    check_feedback_path,
    preflight_check_instruments,
)

IDLE = "ожидание"
CONNECTING = "проверка приборов"
SEEDING = "бинарный поиск"
STABILIZING = "грубая стабилизация"
FINE_TUNING = "точная подстройка"
HOLDING = "удержание — ток стабилен"
PAUSED = "стенд приостановлен"
ERROR = "ошибка"

MAX_SATURATED_RETRIES = 10


class BenchController:
    def __init__(self, cfg):
        self.cfg = cfg
        self.supply = None
        self.feedback_dmm = None
        self.measurement_dmm = None
        self.controller = None

        self.state = IDLE
        self.error_message = None
        self.live_feedback_a = None
        self.live_output_a = None

        self._events = queue.Queue()
        self._stop_flag = threading.Event()
        self._thread = None
        # Измерительный DMM читают И фоновый поток удержания (для live-индикации),
        # И measure_async() по клику "Измерить" — это два разных потока на один и
        # тот же VISA-ресурс, без блокировки могли бы столкнуться на одном порту.
        self._measurement_lock = threading.Lock()

    def _set_state(self, state, message=""):
        self.state = state
        self._events.put(("state", state, message))

    def poll_events(self):
        """Вызывается из UI-потока: забирает все накопившиеся события без блокировки."""
        events = []
        while True:
            try:
                events.append(self._events.get_nowait())
            except queue.Empty:
                break
        return events

    def start(self, setpoint_a):
        if self._thread and self._thread.is_alive():
            return
        self._stop_flag.clear()
        self._thread = threading.Thread(target=self._run, args=(setpoint_a,), daemon=True)
        self._thread.start()

    def _run(self, setpoint_a):
        try:
            if self.supply is None:
                self._set_state(CONNECTING, "Подключение к приборам...")
                self.supply, self.feedback_dmm, self.measurement_dmm = build_instruments(self.cfg)
                if self.cfg["mode"] != "fake":
                    preflight_check_instruments(self.supply, self.feedback_dmm, self.measurement_dmm)
                self.controller = build_controller(self.cfg, self.supply, self.feedback_dmm)

            self.controller.setpoint = setpoint_a

            if self.cfg["mode"] != "fake":
                self._set_state(CONNECTING, "Проверка целостности цепи ОС...")
                i_psu, i_dmm = check_feedback_path(self.supply, self.feedback_dmm)
                self._events.put(("feedback_check", i_psu, i_dmm))

            current_limit = self.cfg["current_loop"].get("current_limit_a", setpoint_a * 1.5)
            self.supply.set_current_limit(current_limit)
            self.supply.enable_output(True)

            if hasattr(self.controller, "coarse_seed"):
                self._set_state(SEEDING, "Бинарный поиск стартового напряжения...")
                self.controller.coarse_seed()

            if not self._stabilize_loop(setpoint_a):
                return  # остановлено через pause() до сходимости

            fine_deadband = self.cfg["current_loop"].get("fine_deadband_a")
            fine_timeout = self.cfg["current_loop"].get("fine_timeout_s", 0.0)
            if fine_deadband is not None and fine_timeout > 0:
                if not self._fine_tune_loop(fine_deadband, fine_timeout):
                    return

            self._set_state(HOLDING, "Ток стабилен, удержание уставки.")
            while not self._stop_flag.is_set():
                current = self.controller.step()
                self._update_live(current)

        except Exception as exc:
            self.error_message = str(exc)
            self._set_state(ERROR, str(exc))
            try:
                if self.supply:
                    self.supply.enable_output(False)
            except Exception:
                pass

    def _stabilize_loop(self, setpoint_a):
        self._set_state(STABILIZING, "Грубая стабилизация...")
        window = []
        timeout = self.cfg["current_loop"]["timeout_s"]
        start = time.monotonic()
        tried_narrow = tried_full = False

        while time.monotonic() - start < timeout:
            if self._stop_flag.is_set():
                return False
            current = self.controller.step()
            self._update_live(current)
            if self.controller.is_stable(current, window):
                return True
            if getattr(self.controller, "saturated_steps", 0) >= MAX_SATURATED_RETRIES:
                if not tried_narrow and hasattr(self.controller, "reseed_from_current"):
                    self.controller.reseed_from_current()
                    window = []
                    tried_narrow = True
                    continue
                if not tried_full and hasattr(self.controller, "coarse_seed"):
                    self.controller.coarse_seed()
                    window = []
                    tried_full = True
                    continue
                raise RuntimeError(
                    f"Напряжение упёрлось в потолок {self.controller.max_voltage}В, "
                    f"ток ({current*1000:.4f} мА) далёк от уставки ({setpoint_a*1000:.3f} мА) "
                    "даже после пересева. Проверь подключение датчика."
                )
        raise TimeoutError("Ток не стабилизировался за отведённое время.")

    def _fine_tune_loop(self, fine_deadband, fine_timeout):
        self._set_state(FINE_TUNING, "Точная подстройка...")
        fine_window = []
        fine_start = time.monotonic()
        while time.monotonic() - fine_start < fine_timeout:
            if self._stop_flag.is_set():
                return False
            current = self.controller.step()
            self._update_live(current)
            if self.controller.is_stable(current, fine_window, deadband=fine_deadband):
                break
        return True  # не успели точнее — не ошибка, остаёмся на базовой точности

    def _update_live(self, feedback_current):
        self.live_feedback_a = feedback_current
        with self._measurement_lock:
            try:
                self.live_output_a = self.measurement_dmm.measure_dc_current()
            except Exception:
                pass
        self._events.put(("live", self.live_feedback_a, self.live_output_a))

    def measure(self, n_samples=5):
        """Блокирующее измерение — n_samples отсчётов измерительного DMM, пока
        петля ОС продолжает держать ток В ФОНЕ (разные физические приборы, не
        мешают друг другу). Не проверяет state — читает то, что есть на данный
        момент, оператор сам решает по статусу, стабильно ли уже показание."""
        if self.measurement_dmm is None:
            raise RuntimeError("Стенд не запущен — сначала 'Включить уставку'.")
        samples = []
        with self._measurement_lock:
            for _ in range(n_samples):
                samples.append(self.measurement_dmm.measure_dc_current())
        mean = sum(samples) / len(samples)
        std = (sum((x - mean) ** 2 for x in samples) / len(samples)) ** 0.5
        sem = std / (len(samples) ** 0.5)
        return {
            "samples": samples,
            "mean_a": mean,
            "std_a": std,
            "sem_a": sem,
            "feedback_a": self.live_feedback_a,
            "accuracy": compute_relative_error(mean, self.controller.setpoint),
            "ts": time.time(),
        }

    def measure_async(self, n_samples=5):
        def _do():
            try:
                result = self.measure(n_samples)
                self._events.put(("measurement", result))
            except Exception as exc:
                self._events.put(("measurement_error", str(exc)))
        threading.Thread(target=_do, daemon=True).start()

    def pause(self):
        """Гасит выход, останавливает фоновый поток удержания — готово к
        переподключению датчика. Приборы (VISA-хендлы) остаются открытыми,
        следующий start() их переиспользует, не переоткрывая заново."""
        self._stop_flag.set()
        if self._thread:
            self._thread.join(timeout=5.0)
        if self.supply:
            try:
                self.supply.enable_output(False)
            except Exception:
                pass
        self._set_state(PAUSED, "Стенд приостановлен — можно переподключать датчик.")

    def close(self):
        self.pause()
        for inst in (self.supply, self.feedback_dmm, self.measurement_dmm):
            if inst is not None and hasattr(inst, "close"):
                try:
                    inst.close()
                except Exception:
                    pass
