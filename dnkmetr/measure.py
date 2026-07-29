"""Итерация 1: цикл измерения по списку датчиков с ручной коммутацией (пауза на Enter).

Автоматическая коммутация (STM32 + реле/мультиплексор) добавляется позже как
замена вызова _wait_for_operator() на switch.select(channel) — остальной код
не меняется (см. PLAN.md, раздел 5).
"""

import csv
import os
import time

from current_control import HardwareCCController, SoftwarePIController
from drivers import FakeMultimeter, FakePowerSupply, Multimeter, PowerSupply


def build_instruments(cfg):
    if cfg["mode"] == "fake":
        supply = FakePowerSupply()
        feedback_dmm = FakeMultimeter(supply)
        measurement_dmm = FakeMultimeter(supply)
        return supply, feedback_dmm, measurement_dmm

    instr_cfg = cfg["instruments"]
    supply = PowerSupply(instr_cfg["power_supply"]["profile"], instr_cfg["power_supply"]["resource"])
    feedback_dmm = Multimeter(
        instr_cfg["feedback_multimeter"]["profile"], instr_cfg["feedback_multimeter"]["resource"]
    )
    measurement_dmm = Multimeter(
        instr_cfg["measurement_multimeter"]["profile"], instr_cfg["measurement_multimeter"]["resource"]
    )
    return supply, feedback_dmm, measurement_dmm


def build_controller(cfg, supply, feedback_dmm):
    loop_cfg = cfg["current_loop"]
    if cfg["controller"] == "hardware_cc":
        return HardwareCCController(
            supply, feedback_dmm,
            setpoint=loop_cfg["setpoint_a"],
            deadband=loop_cfg["deadband_a"],
        )
    return SoftwarePIController(
        supply, feedback_dmm,
        setpoint=loop_cfg["setpoint_a"],
        kp=loop_cfg["kp"],
        ki=loop_cfg["ki"],
        dt=loop_cfg["dt_s"],
        deadband=loop_cfg["deadband_a"],
        max_voltage=loop_cfg["max_voltage_v"],
        slew_rate=loop_cfg["slew_rate_v_per_s"],
    )


def validate_shunt(shunt_ohms, min_ohms, max_ohms):
    """Выход датчика токовый — шунту не нужно попадать в разрешение АКИП (он мерит
    ток напрямую, см. measurement_mode="direct_current"), но он всё равно должен
    быть в разумном диапазоне: слишком большой — токовый выход датчика не может
    его "продавить" при своём ограниченном compliance-напряжении и клипует;
    слишком маленький — вне штатного режима работы выходного каскада."""
    if shunt_ohms <= 0:
        raise ValueError(f"shunt_ohms должен быть положительным, задано {shunt_ohms}")
    if not (min_ohms <= shunt_ohms <= max_ohms):
        raise ValueError(
            f"Шунт {shunt_ohms} Ом вне допустимого диапазона "
            f"[{min_ohms}, {max_ohms}] Ом — проверь sensor_circuit в config.yaml "
            "и сам шунт на стенде."
        )


def check_feedback_path(supply, feedback_dmm, probe_voltage=10.0,
                        current_limit=0.012, min_delta_current=0.0003):
    """Проверяет, что амперметр обратной связи реально стоит в разрыве цепи.

    Приём: сравниваем показания ОС-мультиметра при 0 В и при пробном напряжении.
    Если цепь исправна и мультиметр в разрыве, ток должен заметно вырасти;
    если мультиметр вне цепи (или сама цепь разомкнута) — останется на уровне шума.

    Раньше здесь сверялись показания ОС-мультиметра с собственным амперметром
    источника (GPP-4323), но на стенде выяснилось, что этот встроенный амперметр
    сам по себе грубый — квантуется шагом ~0.1-1 мА и на токах порядка 1-2 мА
    систематически занижает (пример 29.07.2026: RIGOL честно видел 1.56 мА при
    5 В на тестовом резисторе 3 кОм, а источник в это же время показывал
    0.1-0.3 мА) — сравнение с ним как с эталоном давало ложные срабатывания.
    Проверка по приращению самого ОС-мультиметра не зависит от точности
    источника и всё ещё ловит реальный обрыв (см. эпизод 29.07.2026, когда
    RIGOL был физически вне цепи и показывал ~1.3 нА независимо от напряжения).
    """
    supply.set_current_limit(current_limit)
    supply.set_voltage(0.0)
    supply.enable_output(True)
    try:
        time.sleep(0.5)
        i_baseline = abs(feedback_dmm.measure_dc_current())

        supply.set_voltage(probe_voltage)
        time.sleep(1.0)
        i_psu = abs(supply.measure_current())
        i_dmm = abs(feedback_dmm.measure_dc_current())
    finally:
        supply.set_voltage(0.0)
        supply.enable_output(False)

    delta = i_dmm - i_baseline
    if delta < min_delta_current:
        raise RuntimeError(
            f"Амперметр обратной связи не видит тока: при {probe_voltage} В "
            f"показания выросли лишь на {delta * 1000:.5f} мА (с {i_baseline*1000:.5f} "
            f"до {i_dmm*1000:.5f} мА). Похоже на обрыв цепи или мультиметр ОС "
            "вне разрыва токовой петли (клеммы A/мА, не V/Ом-Ω)."
        )
    return i_psu, i_dmm


def measure_one(controller, measurement_dmm, sensor_type, k_coeff,
                 shunt_ohms, measurement_mode, timeout=10.0,
                 max_saturated_steps=10):
    controller.supply.enable_output(True)
    if hasattr(controller, "coarse_seed"):
        # Грубый бисекционный поиск стартового напряжения — на порядок быстрее
        # линейного разгона step()-ом и не оставляет большой начальной ошибки,
        # которую потом пришлось бы долго дотягивать ПИ-регулятором.
        controller.coarse_seed()
    window = []
    start = time.monotonic()
    current = None
    stabilized = False

    while time.monotonic() - start < timeout:
        current = controller.step()
        if controller.is_stable(current, window):
            stabilized = True
            break
        if getattr(controller, "saturated_steps", 0) >= max_saturated_steps:
            controller.supply.enable_output(False)
            raise RuntimeError(
                f"Напряжение упёрлось в потолок {controller.max_voltage} В, а ток "
                f"({current * 1000:.4f} мА) далёк от уставки "
                f"({controller.setpoint * 1000:.3f} мА). Похоже на обрыв цепи или "
                "слишком высокое сопротивление нагрузки."
            )

    if not stabilized:
        controller.supply.enable_output(False)
        raise TimeoutError("Ток не стабилизировался за отведённое время")

    if measurement_mode == "voltage_across_shunt":
        raw_voltage = measurement_dmm.measure_dc_voltage()
        output_current = raw_voltage / shunt_ohms
    elif measurement_mode == "direct_current":
        raw_voltage = None
        output_current = measurement_dmm.measure_dc_current()
    else:
        raise ValueError(f"Неизвестный measurement_mode: {measurement_mode!r}")

    result_value = output_current * k_coeff

    controller.supply.enable_output(False)
    return {
        "current_a": current,
        "raw_voltage_v": raw_voltage,
        "output_current_a": output_current,
        "output_value": result_value,
        "ts": time.time(),
    }


def _wait_for_operator(sensor_name, sensor_type):
    input(f"Подключи датчик '{sensor_name}' (тип {sensor_type}) и нажми Enter...")


def append_csv(csv_path, row, fieldnames):
    file_exists = os.path.isfile(csv_path)
    os.makedirs(os.path.dirname(csv_path) or ".", exist_ok=True)
    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


def run_session(cfg):
    circuit_cfg = cfg["sensor_circuit"]
    shunt_ohms = circuit_cfg["shunt_ohms"]
    validate_shunt(shunt_ohms, circuit_cfg["shunt_min_ohms"], circuit_cfg["shunt_max_ohms"])
    measurement_mode = circuit_cfg["measurement_mode"]

    supply, feedback_dmm, measurement_dmm = build_instruments(cfg)
    controller = build_controller(cfg, supply, feedback_dmm)
    timeout = cfg["current_loop"]["timeout_s"]
    csv_path = cfg["output"]["csv_path"]
    fieldnames = ["sensor_name", "sensor_type", "current_a",
                  "raw_voltage_v", "output_current_a", "output_value", "ts"]

    for sensor in cfg["sensors"]:
        _wait_for_operator(sensor["name"], sensor["type"])
        try:
            i_psu, i_dmm = check_feedback_path(supply, feedback_dmm)
            print(f"[{sensor['name']}] цепь ок: источник {i_psu * 1000:.3f} мА, "
                  f"ОС {i_dmm * 1000:.3f} мА")
            result = measure_one(
                controller, measurement_dmm,
                sensor_type=sensor["type"],
                k_coeff=sensor.get("k_coeff", 1.0),
                shunt_ohms=shunt_ohms,
                measurement_mode=measurement_mode,
                timeout=timeout,
            )
        except (TimeoutError, RuntimeError) as exc:
            print(f"[{sensor['name']}] ОШИБКА: {exc}")
            continue

        row = {
            "sensor_name": sensor["name"],
            "sensor_type": sensor["type"],
            **result,
        }
        append_csv(csv_path, row, fieldnames)
        print(f"[{sensor['name']}] I_петли={result['current_a']*1000:.3f} мА, "
              f"I_датчика={result['output_current_a']*1000:.4f} мА, "
              f"значение={result['output_value']:.4f}")

    print(f"Готово. Результаты в {csv_path}")
