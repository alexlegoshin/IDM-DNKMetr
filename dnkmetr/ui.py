"""Простой интерактивный UI для полуавтоматического промера датчиков (Tkinter,
без сторонних зависимостей). Цикл работы оператора:

    1. Выбрать уставку (5 или 10 мА).
    2. "Включить уставку" — стенд сам проходит бинарный поиск и стабилизацию,
       статус обновляется в реальном времени; ток датчика виден live.
    3. "Измерить" — можно нажимать сколько угодно раз, пока ток стабилен;
       каждый раз берётся несколько отсчётов измерительного DMM с усреднением
       и оценкой погрешности (SEM), результат добавляется в лог на экране.
    4. "Приостановить стенд" — гасит выход, можно переподключать датчик.
       Дальше снова с шага 1/2 — по кругу.

Ничего не пишет на диск (см. README.md/PLAN.md, 12.08.2026: сознательный отказ
от CSV в пользу простого "промерили — посмотрели на экране").
"""

import os
import queue
import sys
import tkinter as tk
from tkinter import messagebox, ttk

import yaml

from bench import BenchController, ERROR, FINE_TUNING, HOLDING, STABILIZING

POLL_MS = 150
SETPOINTS_MA = [5.0, 10.0]

# В собранном PyInstaller-exe (--onefile) __file__ указывает во временную
# распакованную папку (_MEIPASS), а config.yaml/instruments/*.json/картинка
# схемы должны лежать РЯДОМ С EXE, чтобы их можно было редактировать без
# пересборки — поэтому базовая директория берётся от sys.executable, если
# приложение заморожено, а не от __file__.
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

WIRING_DIAGRAM_PATH = os.path.join(BASE_DIR, "wiring_diagram.png")
WIRING_DIAGRAM_DISCLAIMER = (
    "Схема ПРИБЛИЗИТЕЛЬНАЯ — не отображает разъёмы, порядок контактов и т.п.\n"
    "Нужна только для сборки измерительного стенда согласно всем маркировкам\n"
    "на самих датчиках и оборудовании."
)


class App:
    def __init__(self, root, cfg):
        self.root = root
        self.bench = BenchController(cfg)
        self._wiring_window = None
        self._wiring_photo = None  # ссылка нужна, иначе Tk соберёт картинку в мусор

        root.title("ДНК-метр — промер датчика")
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        main = ttk.Frame(root, padding=12)
        main.grid(sticky="nsew")
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)

        # --- уставка ---
        setpoint_frame = ttk.LabelFrame(main, text="Уставка тока возбуждения", padding=8)
        setpoint_frame.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self.setpoint_var = tk.DoubleVar(value=SETPOINTS_MA[-1])
        for ma in SETPOINTS_MA:
            ttk.Radiobutton(
                setpoint_frame, text=f"{ma:.0f} мА", variable=self.setpoint_var, value=ma
            ).pack(side="left", padx=8)

        # --- кнопки ---
        btn_frame = ttk.Frame(main)
        btn_frame.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        self.btn_start = ttk.Button(btn_frame, text="Включить уставку", command=self._on_start)
        self.btn_start.pack(side="left", padx=(0, 6))
        self.btn_measure = ttk.Button(
            btn_frame, text="Измерить", command=self._on_measure, state="disabled"
        )
        self.btn_measure.pack(side="left", padx=6)
        self.btn_pause = ttk.Button(
            btn_frame, text="Приостановить стенд", command=self._on_pause, state="disabled"
        )
        self.btn_pause.pack(side="left", padx=6)
        ttk.Button(
            btn_frame, text="Схема подключения", command=self._on_show_wiring
        ).pack(side="right")

        # --- статус ---
        status_frame = ttk.LabelFrame(main, text="Статус", padding=8)
        status_frame.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        self.status_var = tk.StringVar(value="ожидание")
        self.status_label = ttk.Label(
            status_frame, textvariable=self.status_var, font=("Segoe UI", 12, "bold")
        )
        self.status_label.pack(anchor="w")

        # --- живые показания ---
        live_frame = ttk.LabelFrame(main, text="Показания в реальном времени", padding=8)
        live_frame.grid(row=3, column=0, sticky="ew", pady=(0, 8))
        self.feedback_var = tk.StringVar(value="I возбуждения (петля ОС): —")
        self.output_var = tk.StringVar(value="I датчика (выход): —")
        ttk.Label(live_frame, textvariable=self.feedback_var, font=("Consolas", 11)).pack(anchor="w")
        ttk.Label(
            live_frame, textvariable=self.output_var, font=("Consolas", 14, "bold")
        ).pack(anchor="w")

        # --- лог измерений (только на экране, никуда не пишется) ---
        log_frame = ttk.LabelFrame(main, text="Результаты измерений (эта сессия, не сохраняется)", padding=8)
        log_frame.grid(row=4, column=0, sticky="nsew")
        main.rowconfigure(4, weight=1)
        main.columnconfigure(0, weight=1)
        self.log = tk.Listbox(log_frame, height=10, font=("Consolas", 10))
        self.log.pack(fill="both", expand=True)

        self.root.after(POLL_MS, self._poll)

    # --- обработчики кнопок ---

    def _on_start(self):
        setpoint_a = self.setpoint_var.get() / 1000.0
        self.btn_start.config(state="disabled")
        self.btn_pause.config(state="normal")
        self.bench.start(setpoint_a)

    def _on_measure(self):
        self.bench.measure_async(n_samples=5)

    def _on_pause(self):
        self.btn_pause.config(state="disabled")
        self.btn_start.config(state="normal")
        self.btn_measure.config(state="disabled")
        # pause() дожидается фонового потока (join) — короткая блокировка UI,
        # но это явное "стой прямо сейчас" действие оператора, не проблема.
        self.bench.pause()

    def _on_close(self):
        self.bench.close()
        self.root.destroy()

    def _on_show_wiring(self):
        # Не плодим окна при повторных кликах - если уже открыто, просто поднимаем.
        if self._wiring_window is not None and self._wiring_window.winfo_exists():
            self._wiring_window.lift()
            self._wiring_window.focus_force()
            return

        if not os.path.isfile(WIRING_DIAGRAM_PATH):
            messagebox.showerror(
                "Схема подключения",
                f"Файл не найден: {WIRING_DIAGRAM_PATH}",
            )
            return

        win = tk.Toplevel(self.root)
        win.title("Схема подключения (приблизительная)")
        win.protocol("WM_DELETE_WINDOW", lambda: self._on_close_wiring(win))
        self._wiring_window = win

        ttk.Label(
            win, text=WIRING_DIAGRAM_DISCLAIMER, foreground="red",
            font=("Segoe UI", 10, "bold"), justify="left", padding=8,
        ).pack(anchor="w")

        self._wiring_photo = tk.PhotoImage(file=WIRING_DIAGRAM_PATH)
        ttk.Label(win, image=self._wiring_photo).pack(padx=8, pady=(0, 8))

    def _on_close_wiring(self, win):
        win.destroy()
        self._wiring_window = None
        self._wiring_photo = None

    # --- обновление из фонового потока ---

    def _poll(self):
        for event in self.bench.poll_events():
            kind = event[0]
            if kind == "state":
                _, state, message = event
                self.status_var.set(f"{state}" + (f" — {message}" if message else ""))
                self.status_label.config(
                    foreground="red" if state == ERROR else "black"
                )
                # "Измерить" разрешаем не сразу по клику "Включить уставку", а только
                # когда контур реально начал делать шаги (после бинарного поиска) —
                # иначе можно нажать раньше, чем появится хоть одно живое показание
                # (см. журнал 12.08.2026 — ловили TypeError на None вместо тока).
                can_measure = state in (STABILIZING, FINE_TUNING, HOLDING)
                self.btn_measure.config(state="normal" if can_measure else "disabled")
            elif kind == "feedback_check":
                _, i_psu, i_dmm = event
                self._log(f"цепь ОК: источник {i_psu*1000:.3f} мА, ОС {i_dmm*1000:.3f} мА")
            elif kind == "live":
                _, i_fb, i_out = event
                if i_fb is not None:
                    self.feedback_var.set(f"I возбуждения (петля ОС): {i_fb*1000:8.4f} мА")
                if i_out is not None:
                    self.output_var.set(f"I датчика (выход): {i_out*1000:9.4f} мА")
            elif kind == "measurement":
                _, result = event
                feedback_str = (f"{result['feedback_a']*1000:.3f} мА"
                                if result["feedback_a"] is not None else "—")
                acc = result["accuracy"]
                if acc["computed"]:
                    acc_str = (f"γ={acc['gamma_percent']:.4f}% (ГОСТ 8.401-80, "
                              f"номинал {acc['nominal_a']*1000:.1f} мА, "
                              f"Δ={acc['delta_a']*1000:.4f} мА)")
                else:
                    acc_str = acc["note"]
                self._log(
                    f"измерение: {result['mean_a']*1000:.4f} ± {result['sem_a']*1000:.4f} мА "
                    f"(std={result['std_a']*1000:.4f}, n={len(result['samples'])}, "
                    f"I_петли={feedback_str}) | {acc_str}"
                )
            elif kind == "measurement_error":
                _, message = event
                self._log(f"ОШИБКА измерения: {message}")

        self.root.after(POLL_MS, self._poll)

    def _log(self, text):
        self.log.insert(0, text)


def main():
    # chdir на BASE_DIR — тогда config.yaml и относительные пути профилей внутри
    # него ("instruments/xxx.json", см. drivers.py) резолвятся одинаково что при
    # запуске "python ui.py" из dnkmetr/, что при запуске собранного exe откуда
    # угодно (двойным кликом, ярлыком с другим "Start in" и т.п.).
    os.chdir(BASE_DIR)
    with open("config.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    root = tk.Tk()
    App(root, cfg)
    root.mainloop()


if __name__ == "__main__":
    main()
