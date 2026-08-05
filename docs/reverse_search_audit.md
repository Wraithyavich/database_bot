# Аудит данных обратного поиска

Сформирован: 2026-08-05T14:00:39+03:00

## Сводка

- Уникальных нормализованных артикулов: 29262
- Артикулов с Turbo P/N: 18108
- Артикулов с Vehicle OE/OEM: 9504
- Артикулов с OEM/P/N детали: 6980
- Артикулов одновременно с Turbo P/N и OEM: 5194
- Артикулов без достоверных обратных связей: 6646
- Связей `unknown`: 91001
- Повторных свидетельств, удалённых дедупликацией: 198346
- Осиротевших связей: 0
- Связей с конфликтующими типами: 490
- Явных alias/equivalence: 10
- Артикулов, доступных только через alias: 4
- Неоднозначных compact-ключей: 7

## Семантические правила

Классификация выполняется по источнику и смыслу каталожного поля. Старые `numbers` сохранены для прямого поиска; обратный поиск использует отдельную `part_numbers`. Токены модели, двигателя и размеров не показываются как Turbo P/N или Vehicle OEM.

## Известное ограничение локальных данных

В имеющейся исходной SQLite нет каталога Gasket Kits и нет полной строки `GK-0552`: присутствуют только Turbo P/N `454163-0001` и внешний JRONE `2090-505-034`. Номера `454163-0002` и `2 505 034` не добавлены в рабочую базу догадкой. Полная ожидаемая семантика проверяется отдельной тестовой fixture до появления локального исходного каталога.

## Конфликтующие типы (первые 100)

- `AC-I006` / `VJ32`: turbo_pn, vehicle_oem; итог `turbo_pn`
- `AC-I007` / `VJ36`: turbo_pn, vehicle_oem; итог `turbo_pn`
- `AC-I008` / `VV14`: turbo_pn, vehicle_oem; итог `turbo_pn`
- `AC-I030` / `VX29`: turbo_pn, vehicle_oem; итог `turbo_pn`
- `AC-I036` / `VJ38`: turbo_pn, vehicle_oem; итог `turbo_pn`
- `AC-I038` / `VT10`: turbo_pn, vehicle_oem; итог `turbo_pn`
- `AC-I069` / `VL38`: turbo_pn, vehicle_oem; итог `turbo_pn`
- `AC-I100E` / `VB23`: turbo_pn, vehicle_oem; итог `turbo_pn`
- `AC-I100E` / `VB37`: turbo_pn, vehicle_oem; итог `turbo_pn`
- `AC-I106-1EV` / `VB40`: turbo_pn, vehicle_oem; итог `turbo_pn`
- `AC-I112` / `AS18`: turbo_pn, vehicle_oem; итог `turbo_pn`
- `AC-I122` / `CX98`: turbo_pn, vehicle_oem; итог `turbo_pn`
- `AC-I187E` / `VJ42`: turbo_pn, vehicle_oem; итог `turbo_pn`
- `AC-I191` / `VV17`: turbo_pn, vehicle_oem; итог `turbo_pn`
- `AC-I193` / `VB34`: turbo_pn, vehicle_oem; итог `turbo_pn`
- `AC-T009E` / `17201-0L040`: turbo_pn, vehicle_oem; итог `turbo_pn`
- `B1-003` / `2674A801`: turbo_pn, vehicle_oem; итог `vehicle_oem`
- `BH-C2-2` / `399-0023-031`: component_pn, turbo_pn; итог `component_pn`
- `BH-C6-1` / `399-0015-505`: component_pn, turbo_pn; итог `component_pn`
- `BH-I25-3` / `8518204`: component_pn, turbo_pn; итог `component_pn`
- `BH-I25-3-1` / `8512379`: component_pn, turbo_pn; итог `component_pn`
- `BH-I25-5` / `781232103`: component_pn, turbo_pn; итог `component_pn`
- `BH-K2-1-1` / `179205`: component_pn, turbo_pn; итог `component_pn`
- `BH-K2-10` / `179204`: component_pn, turbo_pn; итог `component_pn`
- `BH-K4-5` / `80000174667`: component_pn, turbo_pn; итог `component_pn`
- `BH-M12-15` / `90118-01060`: component_pn, turbo_pn; итог `component_pn`
- `BH-M12-7` / `49180-01240`: component_pn, turbo_pn; итог `component_pn`
- `BH-M12-7-1` / `49795-97601`: component_pn, turbo_pn; итог `component_pn`
- `BH-M12-9` / `49373-07100`: component_pn, turbo_pn; итог `component_pn`
- `BH-M14-2` / `49188-01500`: component_pn, turbo_pn; итог `component_pn`
- `BH-M15-3` / `49477-01600`: component_pn, turbo_pn; итог `component_pn`
- `BH-M15-5` / `49335-01010`: component_pn, turbo_pn; итог `component_pn`
- `BH-M15-8` / `49189-07802`: component_pn, turbo_pn; итог `component_pn`
- `BH-M17-1` / `49134-00020`: component_pn, turbo_pn; итог `component_pn`
- `BH-M2-20` / `90142-01030`: component_pn, turbo_pn; итог `component_pn`
- `BH-M2-20-1` / `90142-01080`: component_pn, turbo_pn; итог `component_pn`
- `BH-M4-10` / `49178-02800`: component_pn, turbo_pn; итог `component_pn`
- `BH-M6-6` / `49173-02800`: component_pn, turbo_pn; итог `component_pn`
- `BH-M6-7` / `49173-02711`: component_pn, turbo_pn; итог `component_pn`
- `BH-M7-7` / `49135-07100`: component_pn, turbo_pn; итог `component_pn`
- `BH-M8-4` / `49590-45607`: component_pn, turbo_pn; итог `component_pn`
- `BH-S5-8` / `177514`: component_pn, turbo_pn; итог `component_pn`
- `BH-S9-11-1` / `14106-1269`: component_pn, turbo_pn; итог `component_pn`
- `BH-T10-1` / `17201-64060`: component_pn, turbo_pn; итог `component_pn`
- `BH-T10-2` / `17201-64040`: component_pn, turbo_pn; итог `component_pn`
- `BH-T10-3` / `17201-46010`: component_pn, turbo_pn; итог `component_pn`
- `BH-T11-1` / `17201-54010`: component_pn, turbo_pn; итог `component_pn`
- `BH-T13-1` / `17201-11080`: component_pn, turbo_pn; итог `component_pn`
- `BH-T13-2` / `17201-11070`: component_pn, turbo_pn; итог `component_pn`
- `BH-T2-1` / `17201-54030`: component_pn, turbo_pn; итог `component_pn`
- `BH-T5-1` / `17201-30080`: component_pn, turbo_pn; итог `component_pn`
- `BH-T7-1` / `17201-33010`: component_pn, turbo_pn; итог `component_pn`
- `BH-T8-3` / `17201-30010`: component_pn, turbo_pn; итог `component_pn`
- `BM-000B` / `030TC11004000`: turbo_pn, vehicle_oem; итог `vehicle_oem`
- `BM-005B` / `030TC11006000`: turbo_pn, vehicle_oem; итог `vehicle_oem`
- `BM-009BR` / `401TN100144`: turbo_pn, vehicle_oem; итог `vehicle_oem`
- `BV35-R2S` / `5435-970-0038`: component_pn, turbo_pn; итог `component_pn`
- `BV35-R2S` / `5435-970-0043`: component_pn, turbo_pn; итог `component_pn`
- `BV35-R2S` / `5435-970-0045`: component_pn, turbo_pn; итог `component_pn`
- `BV35-R2S` / `5435-970-0060`: component_pn, turbo_pn; итог `component_pn`
- `BV40-R2S` / `5440-970-0010`: component_pn, turbo_pn; итог `component_pn`
- `BV40-R2S` / `5440-970-0013`: component_pn, turbo_pn; итог `component_pn`
- `BV40-R2S` / `5440-970-0019`: component_pn, turbo_pn; итог `component_pn`
- `BV43-001` / `4351-902-001`: turbo_pn, vehicle_oem; итог `turbo_pn`
- `BV43-007` / `4346-902-007`: component_pn, turbo_pn; итог `component_pn`
- `BV43-007` / `5303-988-0068`: component_pn, turbo_pn; итог `component_pn`
- `BV43-024` / `5303-988-0261`: component_pn, turbo_pn; итог `component_pn`
- `BV43-R2S` / `1000-970-0198`: component_pn, turbo_pn; итог `component_pn`
- `BV43-R2S` / `1000-970-0216`: component_pn, turbo_pn; итог `component_pn`
- `BV43-R2S` / `1000-970-0233`: component_pn, turbo_pn; итог `component_pn`
- `BV43-R2S` / `1000-970-0241`: component_pn, turbo_pn; итог `component_pn`
- `BV43-R2S` / `1000-970-0290`: component_pn, turbo_pn; итог `component_pn`
- `BV43-R2S` / `1000-970-0339`: component_pn, turbo_pn; итог `component_pn`
- `BV43-R2S` / `5303-970-0525`: component_pn, turbo_pn; итог `component_pn`
- `BV43-R2S` / `5303-970-0569`: component_pn, turbo_pn; итог `component_pn`
- `BV45-R2S` / `1745-970-0002`: component_pn, turbo_pn; итог `component_pn`
- `BV45-R2S` / `1745-970-0006`: component_pn, turbo_pn; итог `component_pn`
- `BV45-R2S` / `1745-970-0007`: component_pn, turbo_pn; итог `component_pn`
- `BV45-R2S` / `1745-970-0009`: component_pn, turbo_pn; итог `component_pn`
- `BV45-R2S` / `1745-970-0010`: component_pn, turbo_pn; итог `component_pn`
- `BV45-R2S` / `1745-970-0011`: component_pn, turbo_pn; итог `component_pn`
- `BV45-R2S` / `1745-970-0013`: component_pn, turbo_pn; итог `component_pn`
- `BV45-R2S` / `1745-970-0015`: component_pn, turbo_pn; итог `component_pn`
- `BV45-R2S` / `1745-970-0017`: component_pn, turbo_pn; итог `component_pn`
- `BV45-R2S` / `1745-970-0018`: component_pn, turbo_pn; итог `component_pn`
- `BV45-R2S` / `1745-970-0019`: component_pn, turbo_pn; итог `component_pn`
- `BV45-R2S` / `1745-970-0023`: component_pn, turbo_pn; итог `component_pn`
- `CT-VNT` / `17201-0L040`: turbo_pn, vehicle_oem; итог `turbo_pn`
- `CT-YARIS` / `17201-33010`: turbo_pn, vehicle_oem; итог `vehicle_oem`
- `CT-YARIS` / `17201-33020`: turbo_pn, vehicle_oem; итог `vehicle_oem`
- `CT12-000` / `17201-64040`: turbo_pn, vehicle_oem; итог `vehicle_oem`
- `CT12-001` / `17201-64060`: turbo_pn, vehicle_oem; итог `vehicle_oem`
- `CT12-005` / `17201-70010C`: turbo_pn, vehicle_oem; итог `turbo_pn`
- `CT12B-000` / `17201-67010`: turbo_pn, vehicle_oem; итог `vehicle_oem`
- `CT20-001` / `17201-54030`: turbo_pn, vehicle_oem; итог `vehicle_oem`
- `CT20-006` / `17201-54010`: turbo_pn, vehicle_oem; итог `vehicle_oem`
- `CT26-000` / `17201-17010`: turbo_pn, vehicle_oem; итог `vehicle_oem`
- `CT26-000` / `17201-17020`: turbo_pn, vehicle_oem; итог `vehicle_oem`
- `CT26-000` / `17201-17030`: turbo_pn, vehicle_oem; итог `vehicle_oem`
- `CT26-001` / `17201-74010`: turbo_pn, vehicle_oem; итог `vehicle_oem`

## Неоднозначные compact-ключи (первые 100)

- `GK0458`: GK-0458，应该可配齐,GK-0458??????
- `GK0459`: GK-0459，应该可以配齐,GK-0459???????
- `R2S011L`: R2S-011-L,R2S-011-L`
- `RKG541`: RK-G54-1,RK-G5-4-1
- `RKK111`: RK-K11-1,RK-K1-1-1
- `T30002`: T30-002,T300-02
- `TW0201`: TW-020-1,TW-0201

## Принцип безопасности

Сомнительные записи остаются `unknown` и не попадают в стандартную выдачу. Исходные PDF не изменялись, интернет для дополнения номеров не использовался.
