"""Токовая петля: два контроллера с общим интерфейсом step()/is_stable().

Какой использовать — зависит от того, есть ли у источника питания аппаратный
режим CC (Constant Current). См. PLAN.md, раздел 0. Если есть — используй
HardwareCCController (проще и надёжнее). Если нет — SoftwarePIController.
"""

import time


def _window_is_stable(window, size, flatness_tol, setpoint, proximity_tol):
    """Стабильно = окно заполнено И плоское И близко к уставке.

    Проверка одной только "плоскости" окна ложно срабатывает во время
    равномерного разгона (ток ещё далёк от уставки, но меняется на каждом
    шаге примерно на одну и ту же малую величину) — обязательно нужна
    отдельная проверка близости к setpoint.
    """
    if len(window) < size:
        return False
    flat = (max(window) - min(window)) < flatness_tol
    close = abs(window[-1] - setpoint) < proximity_tol
    return flat and close


class HardwareCCController:
    """Источник сам держит ток в CC-режиме, код только читает и ждёт стабилизации."""

    def __init__(self, supply, dmm, setpoint=0.010, deadband=0.0002):
        self.supply = supply
        self.dmm = dmm
        self.setpoint = setpoint
        self.deadband = deadband
        self._armed = False

    def arm(self):
        self.supply.set_current_limit(self.setpoint)
        self._armed = True

    def step(self):
        if not self._armed:
            self.arm()
        return self.dmm.measure_dc_current()

    def is_stable(self, current, window):
        window.append(current)
        if len(window) > 5:
            window.pop(0)
        return _window_is_stable(window, 5, self.deadband * 2, self.setpoint, self.deadband * 3)


class SoftwarePIController:
    """ПИ-регулятор в инкрементной (velocity) форме: на каждом шаге вычисляется
    ПРИРАЩЕНИЕ напряжения, а не абсолютное целевое напряжение.

        dV = Kp*(error - prev_error) + Ki*error*dt

    Явного накопителя интеграла нет — накопление происходит само по себе через
    self._voltage (сумма приращений), и slew-rate limit ограничивает именно то,
    что реально прикладывается, а не то, что "хотел бы" отдельный интегратор.
    Это специально сделано вместо классической position-form PI + back-calculation
    anti-windup: когда slew-limit активен почти на каждом шаге (что здесь норма,
    а не редкое исключение), back-calculation переусердствует с коррекцией и сам
    провоцирует колебания (см. PLAN.md/журнал отладки). В velocity-форме такой
    проблемы нет — приращение и так пропорционально текущей ошибке, а не "прыжку"
    к абсолютной цели.
    """

    def __init__(self, supply, dmm, setpoint=0.010,
                 kp=100.0, ki=1500.0, dt=0.2,
                 deadband=0.0002, max_voltage=1000.0, slew_rate=25.0):
        # Значения по умолчанию подобраны симуляцией на FakeMultimeter (нагрузка ~800 Ом,
        # см. tune_current_loop.py) — сходится за ~2.5 с без перерегулирования на ЭТОЙ
        # модели. На реальном стенде обязательно перетюнить под настоящий датчик и
        # настоящий источник (итерация 2 по PLAN.md) — коэффициенты плана нагрузки датчика
        # заведомо другие.
        self.supply = supply
        self.dmm = dmm
        self.setpoint = setpoint
        self.kp = kp
        self.ki = ki
        self.dt = dt
        self.deadband = deadband
        self.max_voltage = max_voltage
        self.slew_rate = slew_rate

        self._prev_error = 0.0
        self._voltage = 0.0
        self._last_tick = time.monotonic()
        self._saturated_steps = 0

    def _wait_tick(self):
        """Возвращает РЕАЛЬНОЕ время с прошлого шага, а не номинальный self.dt.

        На реальном железе (VISA/serial round-trip) один шаг стабильно занимает
        ~0.4-0.45 с при настроенном dt=0.2с — цикл упирается в задержку
        обмена с приборами, а не ждёт лишнего. Раньше step() использовал
        self.dt как множитель интегральной составляющей и slew-rate лимита,
        из-за чего регулятор "думал", что прошло меньше времени, чем на самом
        деле — интегратор недокручивал вдвое медленнее реального времени, и
        петля стабилизировалась на устойчивом смещении в разы больше требуемой
        точности (см. журнал испытаний 29.07.2026, PLAN.md раздел 9)."""
        now = time.monotonic()
        elapsed = now - self._last_tick
        if elapsed < self.dt:
            time.sleep(self.dt - elapsed)
            now = time.monotonic()
            elapsed = now - self._last_tick
        self._last_tick = now
        return elapsed

    def step(self):
        dt = self._wait_tick()
        current = self.dmm.measure_dc_current()
        error = self.setpoint - current

        delta = self.kp * (error - self._prev_error) + self.ki * error * dt
        self._prev_error = error

        max_dv = self.slew_rate * dt
        delta = max(-max_dv, min(max_dv, delta))

        new_voltage = max(0.0, min(self.max_voltage, self._voltage + delta))
        self._voltage = new_voltage
        self.supply.set_voltage(self._voltage)

        # Упёрлись в потолок напряжения, а ток всё ещё далеко от уставки —
        # признак обрыва цепи или неверного монтажа. Считаем такие шаги подряд,
        # чтобы measure_one() мог оборвать заезд, не дожидаясь общего таймаута.
        at_ceiling = self._voltage >= self.max_voltage - 1e-9
        if at_ceiling and current < self.setpoint * 0.5:
            self._saturated_steps += 1
        else:
            self._saturated_steps = 0
        return current

    @property
    def saturated_steps(self):
        return self._saturated_steps

    def coarse_seed(self, tol_v=0.02, max_iters=25, settle_s=0.5):
        """Бинарный поиск стартового напряжения перед точной ПИ-подстройкой.

        Линейный разгон step()-ом со slew-rate лимитом тратит десяток+ шагов
        только на то, чтобы "нащупать" примерный рабочий диапазон напряжения
        (см. журнал испытаний 29.07.2026 — ~9-13 с до выхода в окрестность
        уставки). Бисекция находит ту же окрестность за log2(диапазон/точность)
        шагов (~10-12 при tol_v=0.02В на диапазоне 0-60В), т.к. ток растёт
        монотонно с напряжением у резистивной/квазирезистивной нагрузки. После
        неё step()/ПИ включается уже почти без ошибки — доводит точность и
        отслеживает медленный тепловой дрейф, а не тратит время на разгон.

        Предполагает МОНОТОННУЮ зависимость тока от напряжения у рабочей точки —
        для резистивного тестового шунта это точно так, для реального датчика
        надо будет перепроверить перед использованием на нём.
        """
        lo, hi = 0.0, self.max_voltage
        v = 0.0
        for i in range(max_iters):
            if hi - lo < tol_v:
                break
            v = (lo + hi) / 2.0
            self.supply.set_voltage(v)
            time.sleep(settle_s)
            current = self.dmm.measure_dc_current()
            if current < self.setpoint:
                lo = v
            else:
                hi = v

        self._voltage = (lo + hi) / 2.0
        self.supply.set_voltage(self._voltage)
        time.sleep(settle_s)
        final_current = self.dmm.measure_dc_current()

        self._prev_error = self.setpoint - final_current
        self._last_tick = time.monotonic()
        self._saturated_steps = 0
        return self._voltage, final_current, i + 1

    def is_stable(self, current, window):
        window.append(current)
        if len(window) > 5:
            window.pop(0)
        return _window_is_stable(window, 5, self.deadband * 2, self.setpoint, self.deadband * 3)
