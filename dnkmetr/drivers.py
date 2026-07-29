"""Универсальный SCPI-драйвер, управляемый JSON-профилями из instruments/*.json.

Никакой конкретный вендор/модель не зашиты в коде — только семантика роли
(power_supply / multimeter): какие команды должны быть в профиле и как
парсить ответ. Сама SCPI-строка ("VOLT {value:.4f}", "MEAS:CURR:DC?" и т.п.)
и параметры соединения (baud rate, терминаторы) приходят из JSON-профиля.
Смена прибора = смена profile+resource в config.yaml, без правок кода.
"""

import json
import random
import re

import pyvisa
from pyvisa.constants import Parity, StopBits

_PARITY = {"none": Parity.none, "odd": Parity.odd, "even": Parity.even}
_STOP_BITS = {1: StopBits.one, 1.5: StopBits.one_and_a_half, 2: StopBits.two}

_NUMBER_RE = re.compile(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")


def _parse_number(reply):
    """Достаёт число из ответа прибора.

    Мультиметры отдают чистый float ('-2.5547888e-07'), а GPP-4323 приписывает
    единицы измерения: 'VOUT1?' -> '00.000V', 'IOUT1?' -> '0.0000A'. Голый
    float() на таком ответе падает, поэтому вытаскиваем первое число регуляркой.
    """
    match = _NUMBER_RE.search(reply)
    if match is None:
        raise ValueError(f"Не удалось разобрать число в ответе прибора: {reply!r}")
    return float(match.group())


class ScpiInstrument:
    def __init__(self, profile_path, resource_name):
        with open(profile_path, "r", encoding="utf-8") as f:
            self.profile = json.load(f)

        self._commands = self.profile["commands"]
        conn = self.profile.get("connection", {})

        self._rm = pyvisa.ResourceManager()
        self.inst = self._rm.open_resource(resource_name)
        self.inst.timeout = conn.get("timeout_ms", 5000)
        self.inst.write_termination = conn.get("write_termination", "\n")
        self.inst.read_termination = conn.get("read_termination", "\n")

        if conn.get("type") == "serial":
            self.inst.baud_rate = conn["baud_rate"]
            self.inst.data_bits = conn.get("data_bits", 8)
            self.inst.parity = _PARITY[conn.get("parity", "none")]
            self.inst.stop_bits = _STOP_BITS[conn.get("stop_bits", 1)]
            # На реальном COM-порту после (пере)подключения в буфере иногда остаётся
            # мусор от предыдущей сессии — без сброса первый query() может словить
            # длинный VI_ERROR_TMO вместо ответа на самом деле исправного прибора.
            self.inst.clear()

    def _cmd(self, name, **kwargs):
        return self._commands[name].format(**kwargs)

    def write(self, name, **kwargs):
        self.inst.write(self._cmd(name, **kwargs))

    def query(self, name, **kwargs):
        return self.inst.query(self._cmd(name, **kwargs)).strip()

    def query_number(self, name, **kwargs):
        return _parse_number(self.query(name, **kwargs))

    def idn(self):
        return self.query("idn")

    def close(self):
        self.inst.close()


class PowerSupply(ScpiInstrument):
    def set_voltage(self, volts):
        self.write("set_voltage", value=volts)

    def set_current_limit(self, amps):
        self.write("set_current_limit", value=amps)

    def enable_output(self, state=True):
        on_value = self.profile.get("output_on_value", "1")
        off_value = self.profile.get("output_off_value", "0")
        self.write("enable_output", state=on_value if state else off_value)

    def measure_voltage(self):
        """Показания собственного вольтметра источника. Не для петли ОС —
        точности хватает только на логирование и sanity-check."""
        return self.query_number("measure_voltage")

    def measure_current(self):
        return self.query_number("measure_current")


class Multimeter(ScpiInstrument):
    def measure_dc_current(self):
        return self.query_number("measure_dc_current")

    def measure_dc_voltage(self):
        return self.query_number("measure_dc_voltage")


class SwitchController(ScpiInstrument):
    """Итерация 3: UART к STM32. Не используется, пока нет платы коммутации."""

    def select_hv_channel(self, n):
        self.write("select_hv_channel", channel=n)

    def select_lv_channel(self, n):
        self.write("select_lv_channel", channel=n)

    def reset(self):
        self.write("reset")


# ---------------------------------------------------------------------------
# Заглушки для разработки/отладки без физического стенда. Профилями не
# управляются намеренно — это модель физики нагрузки, а не SCPI-протокол.
# ---------------------------------------------------------------------------

class FakePowerSupply:
    def __init__(self, load_ohms=800.0):
        self.voltage = 0.0
        self.output_on = False
        # Модель нагрузки живёт здесь, а FakeMultimeter её переиспользует —
        # иначе собственный амперметр источника и мультиметр в петле разошлись бы
        # в показаниях, и check_feedback_path() ложно ругался бы на обрыв.
        self.load_ohms = load_ohms

    def set_voltage(self, volts):
        self.voltage = volts

    def set_current_limit(self, amps):
        pass

    def enable_output(self, state=True):
        self.output_on = state

    def measure_voltage(self):
        return self.voltage if self.output_on else 0.0

    def measure_current(self):
        return self.voltage / self.load_ohms if self.output_on else 0.0

    def idn(self):
        return "FAKE,POWER-SUPPLY,0,1.0"


class FakeMultimeter:
    """Симулирует резистивную нагрузку ~800 Ом с шумом, чтобы петля сходилась к 10 мА."""

    def __init__(self, power_supply, load_ohms=None, noise=0.00005):
        self._supply = power_supply
        self.load_ohms = (load_ohms if load_ohms is not None
                          else getattr(power_supply, "load_ohms", 800.0))
        self.noise = noise

    def measure_dc_current(self):
        if not self._supply.output_on:
            return 0.0
        base = self._supply.voltage / self.load_ohms
        return max(0.0, base + random.uniform(-self.noise, self.noise))

    def measure_dc_voltage(self):
        return self.measure_dc_current() * self.load_ohms * 0.5 + random.uniform(-0.001, 0.001)

    def idn(self):
        return "FAKE,MULTIMETER,0,1.0"
