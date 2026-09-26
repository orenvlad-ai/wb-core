# Временная чистка данных — проверенные случаи

Сопровождение [ручной процедуры](temporary_data_cleanup.md). Исследование WBC0110,
26 сентября 2026. История задач и доступные receipts сверены; production-чистка
при подготовке справки не запускалась. Числа ниже исторические, не текущий допуск.
GB = 10⁹ байт, GiB = 2³⁰ байт; размер payload, allocated blocks и изменение
`df available` различаются. Эффекты разных дат/дисков не суммировать как текущую
экономию. Краткое изложение здесь позволяет использовать опыт без старых чатов;
их IDs и пути ниже служат дополнительными проверочными указателями.

## Реестр

| Случай | Дата | Раздел и действие | Подтверждённый результат |
| --- | --- | --- | --- |
| 0008-A | 25.08 | Root: архивные journald старше 14 дней | 43 файла, 2 749 153 280 B allocated |
| 0008-B | 25.08 | Root: два retired DCP дерева по манифесту | 3 620 061 184 B allocated; вместе A+B прирост available 6 368 485 376 B |
| 0008-C | 26–27.08 | Шесть больших SQLite-копий с root в проверенный warm archive на backup | На root освобождено 27 591 725 056 B allocated; 6 архивов + 6 manifest сохранены |
| 0063 | 08.09 | Backup: две отдельные штатные warehouse retention операции | Native removed_bytes 6 734 636 764 B и 3 381 306 226 B; PR1239 выпущен |
| 0069К2 | 10.09 | Backup: native Finance replacement | Удалён старый набор 26 567 467 008 B allocated после проверки нового; не чистая экономия всего цикла |
| 0069К15-A | 13.09 | Root: 2 APT cache + 186 pip cache files | 188 файлов; available +179 122 176 B |
| 0069К15-B | 13.09 | Root: точный Promo GC временных collector-файлов | 10 080 файлов; payload 8 764 700 498 B; available +8 778 858 496 B |
| 0061 / 0033К2 | 22.09 | Root: native vacuum архивных systemd-журналов до 1 GiB | Available +3 246 030 848 B; итог 26,8612 GiB свободно |
| 0095 | 22.09 UTC / 23.09 ЕКТ | Backup: один старый warehouse checkpoint + manifest | Native removed_bytes 4 578 427 806 B; две новые копии сохранены |
| 0072К2 | 23 и 25.09 | Backup: завершившаяся штатная warehouse ротация | 23.09 около 4,63 GB по отчёту задачи; 25.09 receipt на 4 787 635 102 B; без ручного удаления |
| 0106 | 25.09 | Root: только старые debug traces Promo | 11 154 файла / 4 750 192 608 B payload; non-target и 13 XLSX сохранены |

## 0008: аварийная очистка и отдельное архивирование

На `/dev/sda1` оставалось около 4,04 GB. Для аварийного этапа согласованы только
старые архивные журналы и два retired DCP дерева: `/opt/dev-control-plane-runtime`
и `/opt/wb-core-runtime/backups/dev-control-plane`. Journald vacuum выполнялся
по сроку 14 дней. DCP удаляли пофайлово по проверенному манифесту, без обхода
symlink; пустые каталоги — через rmdir. Холодный архив
`hosted-runtime-and-rollback.tar.gz`, активные службы, monolith, Finance и
шесть recovery-копий сохранили. Общий available: 4 046 471 168 → 10 414 956 544 B.
Исторические временные receipts уже недоступны локально: детализация подтверждена
отчётом исполнителя в исходной сессии, а не повторным live readback.

Следующий самостоятельный этап перенёс шесть буквальных неактивных SQLite-копий
семейств `ff-pool-overhead-backfill`, `buyout-mature-backfill`, `proxy-v4-pr949`,
`proxy-v4-transit-pr995` в
`state/backups/root-warm-archive-wbc0008-006`. Сжатие zstd, fsync archive/manifest,
полное независимое восстановление, SHA/SQLite integrity и проверка неизменности
исходников предшествовали unlink. Сохранились 6 архивов и 6 manifests; temporary,
partial, pending и foreign leftovers — 0. PR1079 завершён; PR1080 позже добавил
предотвращение повторного заполнения, а не повторял архивирование.

Не считать успехом: в аварийном этапе Promo dry-run нашёл 4 384 кандидата,
но apply отклонил изменившийся fingerprint до первого удаления. Promo deleted=0.
Это другой результат, чем успешный GC в 0069К15.

Источники: задача `01a03a0e-7737-7441-96b1-1f7dae03586a`, исходный JSONL
25.08 строки 1014/1018; [PR1079](https://github.com/orenvlad-ai/wb-core/pull/1079),
[PR1080](https://github.com/orenvlad-ai/wb-core/pull/1080). Исторический код
`apps/root_storage_warm_archive.py` и `migration/159_root_storage_warm_archive_wbc0008_006.md`
доступны в Git на `1f2271c2d15ae17681acc37df054b9a2f8efc3a6`; в проверенном
нынешнем main их нет. Нельзя трактовать эти пути как установленный актуальный CLI.

## 0063: warehouse retention и ошибка старого CLI

Резерв backup блокировал PR1239. Через каноническую operational DB штатная
retention сначала удалила два заменённых domain checkpoints с manifests,
затем, после успешного складского обновления, ещё одну заменённую копию.
Fingerprint второй операции пришлось получить заново после изменения expiry;
старый план не применили. Две последние копии сохранили. Единой сопоставимой
серии df для всего интервала нет: сумма removed_bytes не равна чистому росту
свободного места, поскольку между операциями появились новые копии.

Отдельный ошибочный вызов legacy CLI выбрал старый monolith, выполнил
`ensure_schema()` и остановился на stale fingerprint. Он не удалял копии, но
показывает, почему одного названия `dry-run` или правильного runtime-dir мало.
После корректной очистки release receipt имел `deployment_complete=true`;
контрольный складской run `whur_a00ea464c9d89af42d089e9e` — success.

Источники: задача `01a07848-cb61-7922-9f4c-b81373395c4f`,
[PR1239](https://github.com/orenvlad-ai/wb-core/pull/1239), локальные материалы
`WBC_new/outputs/wbc0063_expense_fix/{execution_notes.md,retention_canonical_apply.json,retention_after_apply.json}`.

## 0069К2: именно финансовые резервные наборы

Одно native invocation `f88fdd835eea4a64a19d6a2c8a9252de` скопировало пару
`finance_raw`/`operational`, проверило integrity, FK, logical state, checksum и
isolated restore. Только затем current переключился на
`finance-backup-06d0db8e3120388c67cc`, а прежний
`finance-backup-6820a6d479703309c8ce` удалился через штатный lifecycle.
Исходные бизнес-данные и поколения сохранились.

Итоговый backup available 50 744 610 816 B; расчёт следующей Finance replacement
39 472 271 360 B, запас 11 272 339 456 B. 24/24 assertions PASS, health healthy,
нет pending transactions/cursor mismatch/actionable dead letters; timer вернули
в enabled/active/waiting. Старый набор занимал 26,57 GB, но его удаление после
создания нового не означает чистую экономию 26,57 GB. Подтверждена локальная
rotation capacity, а не полный B6 или off-host disaster recovery.

Неразмеченный/неподключённый `/dev/sdd1` не форматировали. При readback больших
journal fields `journalctl --all` устранил `MESSAGE=null` без повторного запуска
операции. Один current Finance set не является warehouse-правилом «две T2».

Источники: задача `01a085db-c501-75d3-8983-56675da81ee8`; материалы
`WBC_audits/0069-ротация-0054/finance_refresh_20260910/finance_refresh_completion.md`,
`terminal_readback_readonly.json`, `final_independent_acceptance.md`.

## 0069К15: cache и другой, более широкий Promo GC

188 cache-файлов: `/var/cache/apt/pkgcache.bin`, `srcpkgcache.bin` и 186 файлов
`/root/.cache/pip/http-v2`. Проверяли exact manifest, inode/device/hash,
single-link regular files, отсутствие открытых ссылок и APT/pip writers.
Installed packages, apt lists/dpkg state, Playwright binaries и бизнес-данные
сохранили. Available: 26 764 324 864 → 26 943 447 040 B; небольшой запас сверх
25 GiB позволил пройти текущий барьер, но не обещал долгого решения.

Позднее отдельный штатный GC обработал 10 080 файлов
`state/promo_xlsx_collector_runs`. Сохранены архивы (42 normalized из 136 records)
и non-target digest. Available: 26 789 224 448 → 35 568 082 944 B.
Подтверждены отсутствие выбранных файлов и достаточность пика следующей операции.
Это материалы Promo, **не финансовые записи или Finance SQLite**.

Native audit: `state/promo-campaign-archive-gc/3174a15b17144a25c8afc5b9a512dcf96148965a70836dabbe7c6425177133b0.json`.
Источники: задачи `01a0989b-383e-7fe1-b720-7a321d36233d` и
`01a09899-e2c6-7ca1-b858-9cbe6a95ea61`; JSONL исполнителя 13.09 строки
1125/1135/1235/2754/2945/3043; материалы
`WBC_audits/0069-ротация-0054/ОСТАВШИЕСЯ_МЕТРИКИ_12_0082/`:
`cache-apply-receipt-v1.json`, `reviewer-cache-after-PASS.json`,
`promo-gc-apply-v1.json`, `reviewer-promo-gc-after-PASS.json`,
`curator-gc-completion-verification.json`.

## 0061 / 0033К2: journals 22 сентября

Это один эпизод координации, не две независимые очистки. Root был ниже порога.
После отдельного разрешения в 08:08 UTC один native vacuum по
`/var/log/journal` до 1 GiB уменьшил общее journal usage с 4,0G до 994,8M.
Available: 25 541 738 496 → 28 787 769 344 B сразу после операции;
финальное чтение 28 841 988 096 B. Разница последнего замера включает фоновую
активность. Все пять основных служб остались active, новых failed units не было.
Не выполнялись rotate, restart, изменение конфигурации, БД/backup/других логов.
Удалённую архивную диагностику восстановить нельзя.

Источники: исходная задача `01a05c4a-0b90-7763-958f-e69289023d24`, сообщения
6535–6614; исполнитель `01a0c826-126b-7ff3-9579-c3bf8bacc86f`; локальный
`~/.codex/evidence/WBC0033K2-journal-vacuum-2026-09-22.md`.

## 0095: ручной apply одной заменённой копии

22.09 в 20:15 UTC (23.09 01:15 ЕКТ) удалены только checkpoint
`recovery_0d4e572422ac184e4241144b6f27e63c.sqlite3` и его manifest из
`state/backups/warehouse-recovery/domain-checkpoints`.
Checkpoint создан 22.09 16:21 UTC. Native result —
`retention_8d80395c10df8bdf60d5831c`, `status=applied`, errors пусты,
removed_bytes 4 578 427 806. Защищённые две более новые копии повторно сверены
по файлам/digests; новый plan candidates=0, retained_t2_count=2;
backup available 48 000 253 952 B. Recovery schema digest не изменился.

Здесь были explicit active StoreRegistry path, SHA/schema/manifest/identity
проверки и обе warehouse блокировки. Native retention выбрала кандидата по
`projected_byte_cap`, а не просто по возрасту. Старая копия невосстановима;
возможность восстановления обеспечивают две сохранённые в отдельном governed plan.
После снятия storage-блокера следующий выпуск всё ещё мог остановиться по другой
причине (HTTP429 Autoanswers); очистка не равна завершению всех работ.

Источники: задача `01a0b429-34f8-7a50-a7c0-a19563e3b012`, JSONL 18.09
строки 2670/2702/2707/2782/3011 (сессия началась 18.09, операция 22.09 UTC).
Проверенная историческая обвязка на `wbc-codex-1`:
`/srv/wbc/context/wbc-0095-warehouse-retention.py`; содержит зашитые цели,
не предназначена для нового запуска без нового плана и ревью.

## 0072К2: когда достаточно дождаться native retention

23.09 задача зафиксировала штатное удаление старой копии примерно 4,63 GB,
сохранение двух новых и завершение ранее разрешённого выпуска. Точный байтовый
receipt этого эпизода в справку не извлечён: число — округлённое из отчёта задачи.

25.09 в 18:42 UTC подтверждён native run
`retention_2cdd3b65c9673812ff31b50c`: ровно прежний кандидат 4 787 635 102 B,
две protected copies сохранены, новый plan candidates=0. Status-readback exit 0,
alerts пусты, backup available 47 040 266 240 B при required 44 987 232 256 B.
Ручное удаление не выполняли. Это освобождение backup; Promo из 0106 очистил root.

Источники: задача `01a08a9f-84fc-7d82-ac15-3fb052f8f8bf`, JSONL сессии
10.09 строки 12427/12592 и 22056/22069;
`~/.codex/artifacts/wbc-0072k2-batch-20260925/STORAGE-BLOCKER.md`.

## 0106: debug-only Promo 25 сентября

Блокер затрагивал два разных диска. Warehouse-кандидат примерно 4,75 GB
решал бы только backup; root оставался ниже резерва примерно на 1,8 GB.
На root выбрали старую диагностику Promo, сохранив нужную историю недавних сбоев
в journald, browser cache/profiles, legacy archives, БД и все XLSX.

Один точный subset: 10 068 `partial` debug files старше 14 дней и 1 086 `success`
старше 7 дней, всего 11 154 / 4 750 192 608 B. Wrapper проверял root device,
status/возраст всего запуска, отсутствие symlink/hardlink, inode/mtime/size/hash,
границу количества/байтов и non-target digest. Native audit `applied`, deleted_count
11154, errors отсутствуют. Независимая проверка подтвердила отсутствие кандидатов,
неизменность остальных файлов и сохранение 13 XLSX. Свежий storage status:
alerts пусты, root available 29 974 978 560 B; позднее независимое чтение около
29,87 GB. Это изменяющиеся снимки, не две разные очистки.

Audit: `state/promo-campaign-archive-gc/719d1dc98e7f209887214a6f492e3ad72b97d79e704469d6a05d2070f665e25a.json`.
Источники: задача `01a0d761-4f88-7191-95ff-60137d38c65e`, JSONL 25.09
строки 2598/2658/2680/4145; проверяющий `01a0d7f5-c1f3-77e3-b8fd-099b4d0f427d`,
строка 442. Историческая обвязка:
`/opt/wb-core-runtime/state/private-evidence/wbc0106-promo-debug-gc/promo_debug_gc.py`;
при составлении справки прочитана без исполнения. Успешный выпуск и восстановление
метрик впоследствии проверили отдельно; часть исходных source errors осталась.

## Границы исследования

Проверены доступные WBC-сессии сентября, включая недавно продолжавшиеся старые
чаты, целевые августовские материалы №8 и доступные receipts. Это реестр найденных
подтверждённых эпизодов; недоступные истории других аккаунтов не объявляются
проверенными. Не найдено подтверждения, что для чистки удаляли действующие
финансовые бизнес-записи. Финансовый кейс — native replacement резервного набора.
Не предложен общий способ удаления активных поколений или legacy monolith.

Идентификаторы сессий позволяют найти первоисточник в `~/.codex/sessions` по UUID,
независимо от локализованного заголовка. Локальные исходные выгрузки и полные
манифесты не включены в Git; здесь только необходимые итоги и процедура.
