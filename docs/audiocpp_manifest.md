# audiocpp.manifest v1 — контракт бандла (synchronized copy from mcp-audiocpp v1)

> Это **синхронизированная копия** канонического документа `mcp-audiocpp/docs/audiocpp_manifest.md`.
> При изменении контракта обновлять обе копии. Единственное локальное дополнение — раздел
> «Локальные заметки (reaper-сторона)» в конце файла: он описывает только эту сторону и не
> является частью контракта.

Единственный формат обмена между `mcp-audiocpp` (экспорт) и `reaper-reapy-mcp` (применение).

## Инструменты

| Сторона | Инструмент | Роль |
|---|---|---|
| `mcp-audiocpp` | `export_audiocpp_manifest` | собирает бандл из тейка (wav+score+events+semantic) |
| `reaper-reapy-mcp` | `apply_audiocpp_manifest` | читает бандл и заполняет текущий проект REAPER |

## Структура бандла

```
<любая папка>/
├─ manifest.json      ← единственный источник правды
├─ song.wav           ← копия аудио (имя из manifest.audio.file)
├─ vocal.mid          ← MIDI голоса (свежий, из score)
├─ ins.mid            ← MIDI инструментала
├─ score.abc          ← копия партитуры (история/правки)
├─ semantic.json      ← опционально (для будущих continue; по имени файла)
└─ apply_report.json  ← создаёт apply-сторона после применения
```

## manifest.json

```json
{
  "schema": "audiocpp.manifest",
  "version": 1,
  "created_utc": "2026-10-11T18:00:00Z",
  "title": "dnb_horde",
  "bpm": 172.0,
  "meter": [4, 4],
  "key": "D#m",
  "duration_s": 269.96,
  "audio": {"file": "song.wav"},
  "midi": {"vocal": "vocal.mid", "ins": "ins.mid"},
  "regions": [
    {"name": "intro", "start_s": 0.0, "end_s": 11.26, "source": "plan"}
  ],
  "stems": {},
  "provenance": {"producer": "mcp-audiocpp", "tool": "song_generate", "model": "yue2_q4", "quant": "q4_0", "seed": 1234},
  "warnings": [],
  "notes": ""
}
```

## Правила

- Пути в манифесте — **только относительные имена файлов** (бандл переносимый).
- `schema` обязан быть ровно `"audiocpp.manifest"`, `version` ровно `1` — иначе отказ (fail-closed).
- `audio.file` обязателен и файл обязан существовать. `midi.vocal`/`midi.ins` — обязательные ключи,
  файлы обязаны существовать (голос с 0 нот всё равно пишется, факт попадает в `warnings`).
- `bpm > 0`; `regions` непустой; каждый регион `start_s < end_s`; `source` ∈ `{plan, audio, mixed}`.
- `duration_s` = длительность аудио (soundfile) — **авторитетна для клампа регионов на apply-стороне**.
- Секунды — float; `meter` — `[num, den]` (int); файлы UTF-8 без BOM.
- Незнакомые поля: apply-сторона игнорирует, но не отвергает (forward-compat).
- `apply_report.json` пишет только применяющая сторона (перезапись при каждом применении).

## Указатель «последний бандл»

- Export после полной записи бандла атомарно (`tmp` + `os.replace`) обновляет
  `work\_latest_manifest.txt` — одна строка с **абсолютным** путём папки бандла.
- Apply при `bundle_dir="latest"` читает этот файл. Переопределение пути:
  env **`AUDIOCPP_MANIFEST_POINTER`**. Указателя нет → внятный отказ.

## Экспорт (mcp-audiocpp)

Резолв: явные пути (`wav_path`/`score_path`/`events_path`/`semantic_path`) побеждают `take_dir`
(ищет `song.wav` / `score.abc` / `events.json` / `semantic.json`). `score` обязателен.
Регионы: бар-математика плана + снап к аудио-якорям из events (окно 8 с; снап > 2 с → `source="audio"` + warning).
Предупреждения манифеста: выход региона за `duration_s` (кламп на apply), расхождение план/аудио > 2 %, голос с 0 нот,
плюс сквозные warnings снапа. Порядок записи: файлы → `manifest.json` → указатель (атомарно).

## Применение (reaper-reapy-mcp)

См. `apply_audiocpp_manifest` в reaper-reapy-mcp (load/validate → dry_run → мутации → verify → `apply_report.json`).
Регионы создаются через `RPR.AddProjectMarker(0, 1, s, e, name, -1)` (MCP create_region исторически не сохраняется).
`region_end = min(end_s, duration_s)`; MIDI с ведущей тишиной не подрезать.

## Записи проверки (E3, 2026-10-13)

Живой прогон в НОВОМ проектом табе (проект пользователя — 10 треков, id `(ReaProject*)0x23359B0` —
не изменён: те же имена треков, bpm 120, 0 регионов до и после):

| Шаг | Результат |
|---|---|
| dry_run | 0 мутаций, план: 5 треков, 12 регионов, кламп `interlude` 281.86→269.959 |
| apply (свежий проект) | `ok:true`, 12/12 регионов, `meter_how`: «added a tempo/time-signature marker at 0.0 s» |
| повторный apply без `clean` | отказ гварда: «the project already contains AI_\* tracks» + hint `clean=true`; проект не изменён |
| apply с `clean=true` | удалено 12 регионов + 5 треков, создано заново, `meter_how`: «edited the tempo/time-signature marker covering 0.0 s» |
| независимая сверка | `AI_MASTER` wav: pos 0.000, length **269.959**; `MIDI_VOCAL`: **289** note-событий; `MIDI_INS`: **214** — совпадает с экспортом E1 |
| `save_as` | `tmp\manifest_smoke.rpp` создан (31 764 байта), внутри абсолютный путь `FILE "…\song.wav"` и 13 имён (титульный маркер + 12 регионов) |
| `apply_report.json` | записан в бандл (2 658 байт) |

---

## Локальные заметки (reaper-сторона)

Этот раздел — про реализацию применения в ЭТОЙ репе (не часть контракта). Два ограничения
измерены живыми прогонами E3, оба обойдены в коде:

1. **Имена маркеров/регионов через reapy не читаются.** `RPR.EnumProjectMarkers2` объявляет
   `char** nameOut`, который reapy заполнить не может: имя всегда приходит `''` (и при `""`, и при `0`
   в этом аргументе). Имя при этом **записано**: созданный регион виден в сохранённом `.rpp` как
   `MARKER 1 0.02 intro 1 0 1 R {GUID}`, а тот же вызов API возвращает `''`. Поэтому идентичность
   региона в `apply` — диапазон `(start_s, end_s)` с допуском 0.1 с, а не `(name, start_s)`.
2. **`TimeMap_SetTimeSigAtTime` отсутствует в сгенерированном API reapy** (контроллерный
   `set_project_time_signature` падает с `AttributeError`, что видно в логе). Рабочий путь из двух
   измеренных вызовов: нет ни одного tempo-маркера → `AddTempoTimeSigMarker(proj, 0.0, bpm, num, den, False)`;
   маркер есть → `SetTempoTimeSigMarker(proj, **-1**, 0.0, -1, -1, bpm, num, den, False)`.
   **`ptidx` обязан быть `-1`**: с `ptidx=0` вызов возвращает 1 и не меняет ничего.
