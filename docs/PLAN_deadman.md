# 計画書 ―― 沈黙検知（deadman / liveness）モジュール

> **これは何**：CloseCraw に欠けている「静かに壊れた物」を捕まえる片割れの実装計画。
> 既存の CloseCraw は **閾値超過**（悪い値が現れる）を Prometheus で検知する。本モジュールは逆に
> **信号の不在**（出るべき物が出ない＝沈黙）を検知する。両者を足して「うるさい故障」と「静かな故障」の
> 両方を同じ通知路（Telegram）で拾えるようにする。
> **設計思想は DESIGN.md を継承**：AI に判断させない・実行は許可済みスクリプトのみ・LLM は要約だけ・
> LLM が死んでも通知は必ず出す。本モジュールは新しい思想を持ち込まず、既存の思想に沈黙を足すだけ。

---

## 1. 背骨 ―― 生存の deny-by-default

普通の監視は「エラーが出たら教えて」。本モジュールは反転させる：

> **load-bearing な工程は、締切内に「生きてる証」を出さない限り、死んでいるとみなす。**

silent 故障は "悪い値の存在" ではなく "信号の不在" として現れる。閾値監視は不在を捕まえられない
（越える値がそもそも来ないから）。だから不在を一級市民として扱う。これは CloseCraw の
「AI に自由度を与えない・決定的に処理する」思想の、監視対象側への延長である。

---

## 2. スコープ（確定）

| | 内容 |
|---|---|
| **IN ①** | **受動優先**。既存ジョブに一切手を入れない。ログ・出力ファイルの mtime・外部 API が既に残す痕跡だけを読んで「最後に生きていた時刻」を推す。 |
| **IN ②** | 監視対象は **少数精鋭 2 系統**：(a) 放送プロセスの生死＝中身を出し続けているか (b) 予約公開／定時ジョブの成否＝予定が本当に実行されたか。 |
| **IN ③** | 通報は **状態盤（read-only）＋ 単一のうるさい push（Telegram・既存 notifier.py 再利用）**。 |
| **IN ④** | **MTTD（沈黙→検知までの時間）を最初から計測・記録**する。正式な比較ベンチは後フェーズ。 |
| **OUT** | 能動心拍（各ジョブに `heartbeat()` を仕込む）＝受動で穴が残った所だけ後付け。 |
| **OUT** | 自動修復・自動再起動。**気づく→調べる→要約→提案まで。直すのは人間**（DESIGN.md の権限最小方針）。 |
| **OUT** | ①の全カテゴリ網羅（ハード腐敗・トークン失効・外向き probe 等）＝筋は良いが MVP では外す。 |

---

## 3. アーキテクチャ ―― 新規部品はプローバ一個

沈黙を **「もう一つの alertname」** として既存パイプラインに流し込む。CloseCraw の下流
（対応表→調査スクリプト→LLM 要約→Telegram）を 100% 再利用する。

```
[liveness_prober.py]  ★唯一の新規部品（systemd timer で定期実行）
   ・受動で痕跡を読む（ログ mtime / 出力ファイル mtime / 外部 API）
   ・対象ごとに「最後に生きていた時刻」を算出
        ↓ Prometheus textfile collector へ書き出し（atomic）
   closecraw_liveness_last_ok_seconds{target="broadcast"}  ...
        ↓
[Prometheus]  ← 既存
        ↓ staleness ルールで発火：time() - last_ok > deadline
[Alertmanager]  ← 既存（新 alertname: BroadcastSilent / ScheduledPublishMissed / TimerSkipped）
        ↓ Webhook
[alert_handler.py :5001]  ← 既存（変更ほぼ無し）
        ↓ ALERT_TO_SCRIPT に沈黙用の行を足す（DESIGN.md 記載の拡張手順どおり）
[調査スクリプト（読み取り専用・新規数本）]  ← 沈黙の文脈を集める
        ↓
[LLM 要約（既存）]  ← 集めた事実を 2〜4 文に。落ちたら生出力で通知（フォールバック維持）
        ↓
[notifier.py → Telegram]  ← 既存
```

**新規で書くのは：**
1. `src/liveness_prober.py`（受動プローバ＋textfile 書き出し）
2. 沈黙用の staleness ルール（Prometheus / Alertmanager 設定・別ディレクトリ管理は既存流儀）
3. 沈黙用の調査スクリプト数本（`scripts/` にホワイトリスト追加）
4. `ALERT_TO_SCRIPT` への行追加（alert_handler.py）
5. 状態盤（read-only・§5）

**変更しない：** notifier.py / LLM 抽象層 / 対応表の仕組み / フォールバック設計。

---

## 4. 監視対象 MVP（具体）

いずれも受動＝既存の痕跡を読むだけ。締切（deadline）は初期値・運用で調整。

| alertname | 生存の証（受動ソース） | 締切の考え方 | 沈黙が意味すること |
|---|---|---|---|
| `BroadcastSilent` | **確定**：`:8100/api/status` を受動 GET（`streaming`/`obs_connected`/`relay_legs.*.up`）でゲート＋一次信号。live 録画 `.mkv` の成長を凍結クロスチェック（制御が "OK" でもエンコーダ凍結を捕まえる） | `streaming` 中に obs 断／relay leg down、または `.mkv` が D 秒成長せず（例 90s） | 配信すべき時に届いていない／録画が凍結（OBS・relay・エンコーダ死） |
| `ScheduledPublishMissed` | 予約公開の予定時刻（スケジュール）× 外部 API 実測の公開状態 | 予定時刻＋猶予（例 +15 分で未公開） | 予約が黙って公開されていない（トークン失効・API 変更等） |
| `TimerSkipped` | 定時ジョブの「今日やったか」（成果物サイドカー等の有無）× 予定表 | 予定時刻＋猶予 | 定時ジョブが黙ってスキップ／実行したが何もしていない |

> **未登録＝未監視**だが、登録は「痕跡ソース＋締切」の 1 行。deny-by-default は登録側にも効かせる：
> 「生存契約を持たない load-bearing 工程」をプローバが警告候補として一覧に出す（穴の可視化）。

---

## 5. 状態盤（read-only）

- 全対象の生死を一枚に：**緑（締切内に生存）／黄（締切近い）／赤（沈黙＝発火）**。
- 実装は既存の read-only ボード流儀に合わせる（新しい常駐を増やさず、既存の盤に相乗りできれば相乗り）。
- **盤は受動＝見に行かなければ意味が薄い**。だから赤への遷移は必ず **Telegram を一発**叩く（§3）。
  盤は「今どうなっているか」、push は「今まさに変わった」を担う。

---

## 6. 「AI に判断させない」の維持（DESIGN.md 整合）

沈黙でも設計思想は一切緩めない。

| AI がやること | AI がやらないこと |
|---|---|
| 調査スクリプトが集めた事実を 2〜4 文に要約する | どの調査スクリプトを走らせるか選ぶ（`ALERT_TO_SCRIPT` 表引き） |
| | 沈黙に対して自動で再起動・修復する（人間承認） |

- alertname は自分で書いた staleness ルールで閉じる（未知の沈黙アラートは来ない）＝表引きで 100% カバー。
- LLM が死んでも**通知は必ず出す**（調査スクリプトの生出力を添付）。「要約は死んでも、沈黙を知らせる仕事は生き残る」。

---

## 7. 評価と検証 ―― MTTD（反証可能・再現可能）

本モジュールの効き目は主観でなく数字で示す。

- **指標＝MTTD**（Mean Time To Detection）：沈黙が始まった時刻 → 検知して通知した時刻の差。
- **最初から計測を刻む**：プローバが沈黙を検知するたび `(検知時刻・推定沈黙開始時刻・target)` をログに残す。
- **再現ベンチ（後フェーズ）**：合成故障を注入する（心拍ファイルの touch を止める／出力を止める）。
  検知までの遅延を測り、**素朴なログ tail 型アラート**と比較する。
- **反証可能な主張（公表候補）**：
  > 「deadman 型の staleness 監視は、ログ tail 型通知より silent 故障の MTTD を N% 縮める。
  > 締切設定 D と痕跡ソース S の下で測定。外れたら棄却。」
  誰でも回せる注入ベンチ＝再現可能。この一本が本モジュールの検証の背骨。

---

## 8. 掟チェック（この計画が守れているか）

- [x] **deny-by-default**：生存の証が無ければ死とみなす（生存側にも登録契約を要求）。
- [x] **AI に判断させない**：選択は表引き・実行は許可スクリプトのみ・AI は要約だけ。
- [x] **通知は必ず出る**：LLM が死んでもフォールバックで沈黙を知らせる。
- [x] **ローカル完結**：成果物を他機に預けない。プローバは対象と同一環境で動く。
- [x] **無料**：既存の ollama/Gemini 切替＋既存 Telegram を再利用。新規課金ゼロ。
- [x] **atomic・冪等**：textfile 書き出しは temp→rename の原子書込。プローバは何回走らせても同じ状態。
- [x] **二重正本にしない**：沈黙は「対応表に行を足す」既存拡張点に乗る。新しい別系統を作らない。

---

## 9. フェーズ（縦切り・各フェーズが単体で出荷可能）

**Phase 0 ―― 一本を端まで貫く（最小の縦切り）**
- 対象は `BroadcastSilent` **一つだけ**。
- `liveness_prober.py` が放送出力の mtime を読む → textfile → staleness ルール →（既存パイプライン）→ Telegram。
- 調査スクリプト 1 本（放送プロセスの状態・直近ログ・GPU/CPU の忙しさを読み取り専用で集める）。
- **達成条件＝わざと放送を止めて、締切後に Telegram が鳴る**（＝沈黙を初めて一件捕まえた）。→ **✅ 達成（2026-08-22・durandal）**：手動実発火＋常駐自鳴化まで完了（詳細は §11 の 3C/3D）。

**Phase 1 ―― 対象を精鋭 2 系統に広げる＋盤**
- `ScheduledPublishMissed` と `TimerSkipped` を追加（受動ソース＋staleness ルール＋調査スクリプト＋対応表行）。
- 状態盤（§5）を立てて緑/黄/赤を一枚に。
- **達成条件＝三種の沈黙が盤に出て、赤で push が飛ぶ**。

**Phase 2 ―― 検証を締める**
- MTTD 注入ベンチ（§7）を実装し、ログ tail 型と比較。
- 反証可能な主張を一本、再現可能な形で書き出す（公表候補）。
- **達成条件＝数字で「効いた／効かない」を言える**。

---

## 10. 未確定（次で詰める）

- **締切 D の初期値**：各対象の通常間隔を実測してから置く（推測で置かない）。
- **状態盤の置き場**：既存の read-only 盤に相乗り vs 独立。相乗りできるか要確認。
- ~~放送の "生存の証" の選定~~ **確定（2026-08-21・実測）**：`:8100/api/status` を受動 GET でゲート＋一次信号（`streaming`/`obs_connected`/`relay_legs.*.up`）、live `.mkv` 成長でクロスチェック。「予約枠が黙って開かなかった（scheduled-but-not-live／要 systemd timer 表）」と「録画は回るが中身が黙る（show_log は番組ごとに別＝要マッピング）」は Phase 1 へ。
- **`:8100` の常駐性**：スロット中のみ起動か常時常駐かを確認（未達＝off-air なのか制御プレーン死なのかの解釈に効く）。
- **コードネーム**：沈黙を聞き分け角笛で報せる者＝神話系の候補あり（任意・急がない）。

## 11. 進捗と次の一手（Phase 0）
1. ✅ 放送の "生存の証" を確定（2026-08-21）＝`:8100/api/status` ＋ `.mkv` 成長クロスチェック。
2. ✅ 最小プローバ `src/liveness_prober.py`（6分岐を注入テストで検証・`.prom` を atomic 出力・off-air ゲート実機確認）。
3B. ✅ 沈黙時アラートを `notifier` 直結で配線＋`--simulate`/`--alert`＋cooldown。ドライラン文面確認済（`TELEGRAM_BOT_TOKEN` は稼働側 `~/scripts/secrets.env`）。
3C. ✅ **初の実発火（2026-08-22 10:47:59・durandal）**＝`--simulate mkv-frozen --alert` で本人 secrets を読み実送信。`alert=sent`＋Telegram に🔴着弾（＝"人間の画面"まで到達）＋`state.json` に `alerted_at` 記録。**＝§9 Phase 0 達成条件クリア（わざと止めて鳴った）**。
3D. ✅ **常駐自鳴化（2026-08-22・durandal）**＝既導入の system timer（`/etc/systemd/system/closecraw-liveness.{service,timer}`・60s・enabled）へ drop-in（`deploy/closecraw-liveness.service.d/alert.conf` → `/etc/systemd/system/…service.d/alert.conf`）で `--alert`＋`EnvironmentFile=~/scripts/secrets.env` を注入。実効 ExecStart に `--alert` 反映・実 run `Result=success/status=0` 確認。以後、放送 live 中の沈黙は**人手なしで自鳴**。cooldown は復旧で自動再武装（`liveness_prober.py:217` が healthy 時 `alerted_at` を pop）。
3A. ✅ 本筋：Prometheus/Alertmanager スタックへの載せ替え（2026-08-22）＝node_exporter の textfile collector で `.prom` を出し、staleness ルール（`deploy/` の BroadcastSilent / BroadcastProberStale）→ Alertmanager → CloseCraw の経路で発火を実証。**その後 2026-09-15 に BroadcastSilent は撤去**＝プローバ直結（`--alert`）が同じ事象を即時・中身付きで送っており、ルール経由は 5 分遅れの重複通知にしかならなかったため（d3522b7）。プローバ自身の死活（BroadcastProberStale）はルール側に残す。`:8100` の常駐性の確認は §10 に残す。
4. ⬜ Phase 1：`ScheduledPublishMissed`／`TimerSkipped`／状態盤／content-silent(show_log)。
   - ✅ **`TimerSkipped`（2026-10-10・durandal）**＝`src/timer_prober.py`。systemd が覚えている痕跡（`list-timers` の最後の発火・`show` の OnCalendar/OnUnitActiveSec/ゆらぎ/有効状態・`systemd-analyze calendar` の発火間隔）だけを読む受動プローバ。判定＝①有効なのに止まっている ②最後の発火から「周期＋ゆらぎ＋精度＋猶予(max(120s, 周期×0.1))」を過ぎても次が来ない ③一度も動かないまま同じ締切を過ぎた。対象外＝一回きり・無効・Condition 不成立（作者の意図どおりの停止）。沈黙に入った/抜けた時だけ Telegram 1 通（変化時のみ）。`.prom` を textfile collector へ。user timer（`deploy/closecraw-timer-prober.{service,timer}`・5 分毎）で常駐。初回実測＝82 本中 74 本を見張り、沈黙 0（誤検知 2 件＝繰り返し間隔の読み落としと Condition 不成立を直した後）。`--simulate <timer>` で沈黙と復旧の通知文を確認（実送信の試験は未実施）。
   - 既知の穴：(a) 一回きりの timer（日付指定）が日付を過ぎても発火しなかった件は見ない (b) 「走ったが何もしなかった」は見ない（成果物の痕跡が要る＝次段） (c) プローバ自身が止まった時の検知は未配線。→ 2026-10-10 bifrost の homelab レーンと合意：durandal は `.prom` を出すだけ、bifrost が鳴らす（前例＝canary・systemd_failed）。homelab 側に `rules/closecraw_alerts.yml` を新設し `TimerProberStale`（`time() - closecraw_timer_prober_run_seconds > 900`・for 0m）と `TimerProberMissing`（absent・for 30m）の 2 本だけ置く。沈黙そのもの（`closecraw_timer_silent`）は bifrost では鳴らさない＝プローバ直結と重複するため（BroadcastSilent 撤去と同じ理由）。✅ 同日、本人の go を受けて homelab レーンが導入（homelab `1ee2e94`・promtool check/test 通過・reload 後に両ルール health ok/inactive を durandal から読み取りで確認）。
   - ✅ **`ScheduledPublishMissed`（2026-10-11・durandal）**＝中身が運用している投稿パイプライン専用になるため、本リポジトリでなく**運用側リポジトリ（`~/scripts/bin/publish-watch`）に置いた**（CloseCraw には汎用の仕組みだけを残す方針）。受動・読むだけ：①SNS 告知の予約＝予約時刻＋猶予を過ぎて投稿記録の無い面 ②動画の予約公開＝公開予定＋猶予を過ぎた物だけ外部 API に 1 回聞き、公開済なら以後聞かない。状態が変わった時だけ通知・15 分毎。下調べの段階で、予約投稿の一面だけが失敗して 3 日放置されていた実例を 1 件捕まえた（手で穴埋め済み）。見ていない＝短尺動画の予約公開・30 日より古い予約。
