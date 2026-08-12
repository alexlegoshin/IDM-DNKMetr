"""Автообнаружение приборов на стенде: какой физический прибор к какому VISA-
ресурсу подключён (по *IDN?), какой профиль ему соответствует, и — для
мультиметров, где по IDN нельзя отличить "тот, что в петле ОС" от "того, что
на выходе датчика" (обе роли использует одна и та же модель, см. журнал
12.08.2026) — куда физически включён каждый экземпляр, по факту поведения при
пробном возбуждении (та же идея, что в measure.check_feedback_path(), только
применённая сразу ко всем кандидатам, а не к одному заранее заданному).

Не заменяет ручные profile+resource в config.yaml — это ОПЦИЯ (instruments:
auto_detect: true), включаемая явно. При выключенном auto_detect ничего в
build_instruments() не меняется.
"""

import glob
import json
import logging
import os
import time

import pyvisa

from drivers import PowerSupply, Multimeter, _DEFAULT_BAUD_CANDIDATES

DEFAULT_INSTRUMENTS_DIR = os.path.join(os.path.dirname(__file__), "instruments")

log = logging.getLogger(__name__)


def load_profiles(instruments_dir=DEFAULT_INSTRUMENTS_DIR):
    """Все *.json профили с непустым idn_match — то есть годные для автораспознавания."""
    profiles = []
    for path in sorted(glob.glob(os.path.join(instruments_dir, "*.json"))):
        with open(path, "r", encoding="utf-8") as f:
            profile = json.load(f)
        if profile.get("idn_match"):
            profiles.append((path, profile))
    return profiles


def _match_profile(idn, profiles):
    idn_upper = idn.upper()
    for path, profile in profiles:
        for pattern in profile["idn_match"]:
            if pattern.upper() in idn_upper:
                return path, profile
    return None, None


def _try_idn(rm, resource, timeout_ms, baud=None):
    """Одна попытка открыть ресурс и получить *IDN?. baud=None — не serial
    (USB0/GPIB/TCPIP, настройки VISA по умолчанию).

    Раньше исключение здесь проглатывалось молча — при перемежающихся сбоях
    связи (см. _probe_idn) это делало причину невозможно понять постфактум,
    оставалась только строка "источник не найден" без объяснения ПОЧЕМУ.
    Теперь полное исключение (тип+сообщение) идёт в лог (см. ui.main(),
    logging настраивается на файл dnkmeter.log рядом с exe/config.yaml).
    """
    try:
        inst = rm.open_resource(resource)
        inst.timeout = timeout_ms
        inst.write_termination = "\n"
        inst.read_termination = "\n"
        if baud is not None:
            inst.baud_rate = baud
            inst.data_bits = 8
            inst.clear()
        reply = inst.query("*IDN?").strip()
        inst.close()
        if not reply:
            log.warning("_try_idn: %s baud=%s — пустой ответ на *IDN?", resource, baud)
            return None
        return reply
    except Exception as exc:
        log.warning("_try_idn: %s baud=%s — %s: %s",
                    resource, baud, type(exc).__name__, exc)
        try:
            inst.close()
        except Exception:
            pass
        return None


def _probe_idn(rm, resource, timeout_ms=3000, retries=2, retry_delay_s=0.3):
    """Пытается получить *IDN? с ресурса без знания, какой это прибор.

    Для ASRL (serial) перебирает стандартные скорости — на этом этапе профиль
    ещё не выбран, поэтому берём общий список кандидатов из drivers.py, а не
    настройку конкретного JSON. Для остальных типов (USB0/GPIB/TCPIP) скорость
    не нужна — открывается с настройками VISA по умолчанию.

    retries=2 на каждую комбинацию (не только на весь перебор бодов целиком) —
    у GPP-4323 на стенде бывают кратковременные пропадания ответа по USB-serial
    (см. README.md, "Симптом 2" в саге про этот прибор): без повтора единичный
    таймаут на правильном бауде выглядел как "источник не найден", хотя прибор
    жив и уже через секунду снова отвечает нормально (эпизод 12.08.2026, вечер).
    """
    bauds = _DEFAULT_BAUD_CANDIDATES if resource.upper().startswith("ASRL") else [None]
    for baud in bauds:
        for attempt in range(retries):
            reply = _try_idn(rm, resource, timeout_ms, baud)
            if reply:
                return reply
            if attempt < retries - 1:
                time.sleep(retry_delay_s)
    return None


def discover_all(instruments_dir=DEFAULT_INSTRUMENTS_DIR, timeout_ms=3000):
    """Возвращает {"power_supply": [...], "multimeter": [...], "unmatched": [...]}.

    Каждая запись в power_supply/multimeter — dict с ключами resource, path,
    profile, idn. unmatched — список (resource, idn_or_none) для того, что
    отозвалось, но ни под один профиль не подошло (или не отозвалось вовсе).
    """
    profiles = load_profiles(instruments_dir)
    rm = pyvisa.ResourceManager()
    resources = rm.list_resources()
    log.info("discover_all: list_resources() -> %s", resources)
    result = {"power_supply": [], "multimeter": [], "unmatched": []}

    for resource in resources:
        idn = _probe_idn(rm, resource, timeout_ms)
        if idn is None:
            log.warning("discover_all: %s не ответил ни на одной попытке", resource)
            result["unmatched"].append((resource, None))
            continue
        path, profile = _match_profile(idn, profiles)
        if profile is None:
            log.warning("discover_all: %s ответил %r, но не подошёл ни под один профиль",
                       resource, idn)
            result["unmatched"].append((resource, idn))
            continue
        entry = {"resource": resource, "path": path, "profile": profile, "idn": idn}
        role = profile["role"]
        result.setdefault(role, []).append(entry)
        log.info("discover_all: %s -> role=%s idn=%r", resource, role, idn)

    return result


def assign_multimeter_roles(power_supply_entry, multimeter_entries,
                            probe_voltage=5.0, current_limit=0.012,
                            settle_s=1.0, min_delta_current=0.0003):
    """Определяет, какой из найденных мультиметров стоит в разрыве петли
    возбуждения (feedback), а какой — на выходе датчика (measurement), по
    факту поведения при пробном возбуждении, а не по одному лишь совпадению
    модели/IDN (обе роли в этом проекте не раз занимала одна и та же модель
    B7-78/1 под разными серийниками, см. config.yaml/README.md).

    Приём — тот же, что в measure.check_feedback_path(), но одновременно для
    ВСЕХ кандидатов: тот, чьи показания заметнее всего вырастут при пробном
    напряжении, физически стоит в петле возбуждения источника — это и есть
    feedback. Остальные (обычно один) достаются measurement по остаточному
    принципу. Датчик при этом может ещё не проводить на пробном токе — так
    что "measurement не увидел роста" ожидаемо и не считается ошибкой,
    в отличие от check_feedback_path(), которая как раз ищет обрыв feedback.

    Если кандидат один — он безальтернативно feedback, measurement остаётся
    не назначен (вызывающий код должен решить, что с этим делать). Если
    кандидатов больше двух — однозначно развести роли автоматически нельзя,
    возвращается None с пояснением через RuntimeError.
    """
    if len(multimeter_entries) == 0:
        raise RuntimeError("Не найдено ни одного мультиметра для назначения ролей.")
    if len(multimeter_entries) == 1:
        return {"feedback": multimeter_entries[0], "measurement": None}
    if len(multimeter_entries) > 2:
        raise RuntimeError(
            f"Найдено {len(multimeter_entries)} мультиметров — автоматически "
            "развести роли feedback/measurement можно только для ровно двух. "
            "Задай profile/resource явно в config.yaml."
        )

    supply = PowerSupply(power_supply_entry["path"], power_supply_entry["resource"])
    dmms = [Multimeter(e["path"], e["resource"]) for e in multimeter_entries]

    try:
        supply.set_current_limit(current_limit)
        supply.set_voltage(0.0)
        supply.enable_output(True)
        time.sleep(0.5)
        baselines = [abs(dmm.measure_dc_current()) for dmm in dmms]

        supply.set_voltage(probe_voltage)
        time.sleep(settle_s)
        probed = [abs(dmm.measure_dc_current()) for dmm in dmms]
    finally:
        supply.set_voltage(0.0)
        supply.enable_output(False)
        supply.close()
        for dmm in dmms:
            dmm.close()

    deltas = [p - b for p, b in zip(probed, baselines)]
    feedback_idx = deltas.index(max(deltas))
    measurement_idx = 1 - feedback_idx

    if deltas[feedback_idx] < min_delta_current:
        raise RuntimeError(
            "Ни один из мультиметров не увидел роста тока при пробном "
            f"напряжении {probe_voltage}В (дельты: {[round(d*1000, 5) for d in deltas]} мА) "
            "— похоже, петля возбуждения разомкнута. Проверь подключение источника."
        )

    return {
        "feedback": multimeter_entries[feedback_idx],
        "measurement": multimeter_entries[measurement_idx],
        "deltas_ma": [d * 1000 for d in deltas],
    }
