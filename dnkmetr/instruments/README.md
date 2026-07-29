# Профили приборов

Один JSON-файл = один прибор (вендор+модель+роль). `drivers.py` не содержит ни одной
SCPI-команды — только семантику роли (`power_supply` / `multimeter`). Смена прибора =
новый JSON здесь + строчка в `config.yaml` (`instruments.<role>.profile` и `.resource`),
без правок кода.

Роль `multimeter` в конфиге используется дважды под разными именами —
`feedback_multimeter` (в контуре ОС, читается на каждом шаге) и `measurement_multimeter`
(разовое чтение выходного сигнала датчика после стабилизации тока) — это два РАЗНЫХ
физических прибора на стенде, не два режима одного и того же класса `Multimeter`.

## Схема

```jsonc
{
  "vendor": "...",
  "model": "...",
  "role": "power_supply" | "multimeter",
  "verified": true | false,        // проверено ли на реальном приборе (см. notes)
  "notes": "...",                  // как проверялось / что уточнить перед использованием

  "connection": {
    "type": "usbtmc" | "serial" | "tcpip",   // "serial" включает baud_rate/parity/stop_bits
    "timeout_ms": 5000,
    "write_termination": "\n",
    "read_termination": "\n",
    // только для type: "serial":
    "baud_rate": 9600,
    "data_bits": 8,
    "parity": "none" | "odd" | "even",
    "stop_bits": 1 | 1.5 | 2
  },

  "commands": {
    // ключи — фиксированные имена, которые ожидает роль (см. ниже), значения —
    // Python format-строки, куда драйвер подставляет value=... / state=... / channel=...
    "idn": "*IDN?",
    "set_voltage": "VOLT {value:.4f}",          // роль power_supply
    "set_current_limit": "CURR {value:.4f}",    // роль power_supply
    "enable_output": "OUTP {state}",            // роль power_supply
    "measure_dc_voltage": "MEAS:VOLT:DC?",       // роль multimeter
    "measure_dc_current": "MEAS:CURR:DC?"        // роль multimeter
  },

  "output_on_value": "1",   // только power_supply — что подставится в {state} при state=True
  "output_off_value": "0"
}
```

## Текущие профили

- `rigol_dm3068.json` — **проверено** вживую (USB-TMC, `*IDN?` отвечает корректно).
- `gwinstek_psu_generic.json` — **проверено** (COM4/ASRL4, 115200 8N1, `*IDN?` →
  `GW INSTEK,GPP-74323,SN:GEZ823770,V1.22`). Команды `set_voltage`/`set_current_limit`/
  `measure_voltage`/`measure_current`/`enable_output` перенесены из проверенного
  `IVtrace_dev/instruments/voltage_sources/gpp74323.json`, но сами по себе (кроме `idn`)
  ещё НЕ дёргались на реальном приборе — сверить при первом реальном включении выхода.
  Устройство определяется в Windows как vendor-specific USB-Serial (VID_2184&PID_0057,
  класс `Class_FF`, НЕ CH340/CH341-совместимый CDC) — нужен официальный драйвер
  "GWInstek GPP-Series Driver Setup" с сайта gwinstek.com, не Zadig и не стоковый CH341SER.
- `akip_b778.json` — **проверено** вживую (USB-TMC, VID_164E/Picotest Corp., `*IDN?` →
  `Prist,V7-78/1,TW00053362,03.45-01-04`). Используется как `measurement_multimeter`.
  Одношаговые `MEAS:VOLT:DC?`/`MEAS:CURR:DC?` протестированы напрямую и работают без
  доп. настройки функции/диапазона — проще, чем в первоисточнике
  (`IVtrace_dev/instruments/multimeters/akipb778.json`, там `SENS:FUNC`+`RANG:AUTO OFF`+`READ?`).
- `akip_2101.json` — **не проверено**: портировано из
  `IVtrace_dev/instruments/multimeters/akip2101.json` про запас (на случай замены АКИП на
  этот, Siglent-платформенный, прибор) — сверить `MEAS:VOLT:DC?`/`MEAS:CURR:DC?` при первом
  реальном подключении именно этой модели.

## Как добавить новый прибор

1. Скопировать ближайший по роли профиль.
2. Подставить реальные SCPI-команды из мануала прибора.
3. Проверить связь: `python -c "from drivers import Multimeter; m = Multimeter('instruments/имя.json', 'VISA-ресурс'); print(m.idn())"`
   (или `PowerSupply` для роли `power_supply`).
4. Прописать `profile`/`resource` в `config.yaml`, поставить `"verified": true` в JSON,
   когда связь подтверждена.
