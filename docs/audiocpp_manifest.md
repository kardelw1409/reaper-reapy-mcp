# audiocpp.manifest v1 — контракт бандла (synchronized copy from mcp-audiocpp v1)

> Это **синхронизированная копия** канонического документа mcp-audiocpp/docs/audiocpp_manifest.md.
> При изменении контракта обновлять обе копии. Локальные заметки reaper-стороны — в конце файла.

Единственный формат обмена между `mcp-audiocpp` (экспорт) и `reaper-reapy-mcp` (применение).
Копия в reaper-репе помечена как synchronized copy — при изменении обновлять обе.

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

## Записи проверки (E1, 2026-10-12)

Live-экспорт DnB-тейка (`out_dnb`): bpm 172, 12 регионов (mixed/plan), 289/214 нот,
duration 269.959 s; warnings: снап хвоста 6.01 с, кламп `interlude` 281.86→269.96, расхождение 4.4 %.

## Записи проверки (E3, 2026-10-13)

Живое применение того же бандла в НОВОМ табе REAPER (проект пользователя не изменён):

- dry_run: 0 мутаций; план 5 треков / 12 регионов / кламп `interlude` 281.86→269.959.
- apply: `ok:true`, 12/12 регионов, tempo 172, meter [4,4] (прочитан обратно `TimeMap_GetTimeSigAtTime`).
- повторный apply без `clean` → отказ гварда + hint `clean=true`; `clean=true` → удалено 12 регионов и
  5 треков, создано заново.
- независимая сверка items: `AI_MASTER` wav pos 0.000 / length 269.959; `MIDI_VOCAL` 289 note-событий;
  `MIDI_INS` 214 — совпадает с E1.
- `save_as` → `.rpp` создан; `apply_report.json` записан в бандл.

## Расширение v1.1 (2026-10-13): сетка, стемы, транскрибированное MIDI

Версия схемы остаётся `1` — все новые поля **опциональные** (старый apply-ридер их игнорирует).

### Зачем: два измеренных свойства рендеров YuE2

1. **У рендера есть lead-in**: первый даунбит аудио — НЕ в 0. Замерено: house **0.42 s** (MuScriptor), DnB **0.12–0.15 s**
   (собственный замер по кик-полосе). Из-за этого регионы «по плану» и волна расходятся с линейкой REAPER.
2. **ABC-партитура — это lead sheet** (мелодия + бас): MIDI из неё беден по определению
   (house: 30 нот вокала; полная транскрипция того же трека даёт 233). Барабанов в ABC нет вообще —
   драм-партия есть только в аудио и извлекается стемами + транскрипцией.

### Новые поля `manifest.json`

```json
{
  "grid": {"bpm": 128.0, "bpm_measured": 127.9869, "source": "muscriptor|measured_phase|score",
           "bar_s": 1.875, "first_downbeat_s": 0.42, "aligned": true, "quantize": "bar|none",
           "max_shift_s": 0.42, "audio_source_offset_s": 0.42, "midi_time_shift_s": -0.42,
           "phase_measure": {"ok": true, "origin_s": 0.42, "hit_fraction": 0.51, "onset_count": 497}},
  "audio": {"file": "song.wav", "start_s": 0.0, "source_offset_s": 0.42},
  "midi": {"vocal": "vocal.mid", "ins": "ins.mid", "drums": "drums.mid", "bass": "bass.mid"},
  "midi_sources": {"vocal": "transcription", "ins": "score", "drums": "transcription"},
  "stems": {"vocals": "stems/vocals.wav", "instrumental": "stems/instrumental.wav",
            "drums": "stems/drums.wav", "bass": "stems/bass.wav", "other": "stems/other.wav"}
}
```

### Правила выравнивания (обе стороны)

- **Времена в манифесте — проектные.** Export сдвигает всё на `origin` (измеренный первый даунбит) и
  **квантует каждую границу региона к ближайшему такту** измеренной сетки (`quantize="bar"`, по умолчанию).
- **Отрицательных позиций в REAPER нет** (замерено: `SetMediaItemInfo_Value(...,'D_POSITION',-0.42)` возвращает 1
  и не меняет ничего) — поэтому:
  - аудио: айтем ставится в `start_s` (0), а лид-ин пропускается через **source offset айтема**
    (`D_STARTOFFS = source_offset_s`, длина укорачивается на столько же);
  - MIDI: события **переносятся внутри файла** на `midi_time_shift_s` (наши артефакты; в тиках, по темпу файла).
- Как получить `origin`: MuScriptor beat detection (если сработал — house 127.99 bpm / 0.42 s) либо
  собственный замер фазы (FFT-полоса 25–120 Гц → флукс → фит фазы; `grid_auto=true`;
  валидирован против MuScriptor: 0.424 vs 0.42, 100 % из 119 киков на сетке).
- Допуск: край региона, попавший в такт, может выходить за конец аудио не более **половины такта** — тогда
  он сохраняется как есть (`region_overhangs` в отчёте), дальше — кламп (как раньше).
- Стемы: `stems.*` копируются в `<bundle>/stems/` (только `.wav`). Apply заполняет базовые `AI_VOCAL`/`AI_INSTR`
  стемами vocals/instrumental и добавляет `AI_DRUMS`/`AI_BASS`/`AI_OTHER`; `midi.drums`/`midi.bass` добавляют
  `MIDI_DRUMS`/`MIDI_BASS`. Без этих полей проект выглядит как v1.0 (5 треков).

### Записи проверки (E5/E6, 2026-10-13)

House (`house_afterglow`, progressive house, Q4_0, seed 777001, 90 s):

| Шаг | Результат |
|---|---|
| стемы (htdemucs, CUDA) | 13.8 s на 90 s аудио |
| транскрипция (MuScriptor small, CUDA) | 18.2 s на 90 s; **drums 309**, bass 160, vocal 233 нот (ABC давал 30) |
| сетка | MuScriptor: **127.99 bpm**, first downbeat **0.42 s** (цель 128) |
| apply (новый таб) | 10 треков, 10 айтемов, `source_offset 0.42` у всех аудио, regions 0/31.875/61.875/76.875, кламп хвоста 89.579, `ok:true` |
| барабаны в проекте | первый кик 15.020 s = **такт 9** (0.42 + 8×1.875) |

DnB (`dnb_horde`, neurofunk, 269.96 s):

| Шаг | Результат |
|---|---|
| стемы | 18.1 s |
| транскрипция | **drums 1176**, bass 614 нот; beat detection MuScriptor **провалился** (bpm null) |
| сетка | собственная: origin **0.153 s**, 51.3 % из 497 онсетов на сетке (нужно 35 %) |
| apply | 10 треков; **12/12 регионов** ровно на тактах 0/8/36/44/64/80/96/104/124/128/145/159; хвост клампнут 269.806 |


---

## Локальные заметки (reaper-сторона)

Этот раздел — про реализацию применения в ЭТОЙ репе (не часть контракта). Четыре ограничения
измерены живыми прогонами, все обойдены в коде:

1. **Имена маркеров/регионов через reapy не читаются.** `RPR.EnumProjectMarkers2` объявляет
   `char** nameOut`, который reapy заполнить не может: имя всегда приходит `''` (и при `""`, и при `0`
   в этом аргументе). Имя при этом **записано** (видно в сохранённом `.rpp` как
   `MARKER 1 0.02 intro 1 0 1 R {GUID}`), поэтому идентичность региона в `apply` — диапазон
   `(start_s, end_s)` с допуском 0.1 с, а не `(name, start_s)`.
2. **`TimeMap_SetTimeSigAtTime` отсутствует в сгенерированном API reapy** (контроллерный
   `set_project_time_signature` падает с `AttributeError`). Рабочий путь: нет ни одного tempo-маркера →
   `AddTempoTimeSigMarker(proj, 0.0, bpm, num, den, False)` (6 аргументов); маркер есть →
   `SetTempoTimeSigMarker(proj, **-1**, 0.0, -1, -1, bpm, num, den, False)`. **`ptidx` обязан быть `-1`**:
   с `ptidx=0` вызов возвращает 1 и не меняет ничего. Чтение метра: `TimeMap_GetTimeSigAtTime(proj, 0.0, 0, 0, 0.0)`
   (5 аргументов) → `[proj, time, num, den, bpm]`.
3. **Отрицательные позиции айтемов невозможны**: `SetMediaItemInfo_Value(item, 'D_POSITION', -0.42)`
   возвращает **1** и НЕ меняет позицию (в плюс пишет исправно: +1.5 → 1.5). Поэтому происхождение
   аранжировки выражается источником: `SetMediaItemTakeInfo_Value(take, 'D_STARTOFFS', offset)` +
   укорачивание айтема на ту же величину (иначе в хвосте остаётся тишина). MIDI — сдвигом событий
   в файлах на стороне экспорта.
4. **Контроллер вставляет по курсору и не ставит позицию** (`insert_audio_item` пишет
   `project.cursor_position = start_time`, затем `InsertMedia(...,0)`), поэтому позиция после вставки
   измеряется (`D_POSITION`) и при расхождении с `audio.start_s` в отчёт идёт предупреждение.

Дополнительно: край региона, попавший в такт, может выходить за конец аудио не более половины такта —
такой край сохраняется (`region_overhangs`), дальше — кламп. `AI_TRACKS` (гвард двойного применения)
включает все опциональные имена: `AI_DRUMS/AI_BASS/AI_OTHER/MIDI_DRUMS/MIDI_BASS`.
