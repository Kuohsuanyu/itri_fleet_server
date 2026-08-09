# itri-fleet-agent

裝在**車輛上位機**(樹莓派 / 板載電腦)的轉發套件。

它訂閱上位機本地 broker 上的 MQTT topic,**原封不動**轉發到車隊伺服器。
它不解析、不對應、不認識任何欄位 —— 所以換一個底盤廠牌不用改任何程式碼。

```
     上位機 (Raspberry Pi)                        伺服器
 ┌──────────────────────────┐
 │ 底盤程式                  │
 │   publish chassis/bat_pct│
 │           chassis/mode   │
 │           sensors/...    │
 │        ↓                 │
 │  本地 broker :1883        │
 │        ↓                 │
 │  itri-agent  訂閱 #       │──── WireGuard ────► fleet/<id>/raw
 └──────────────────────────┘                          ↓
                                            原樣存進 PostgreSQL
                                            伺服器自動編目所有 topic
```

## 安裝

```bash
# 1. 加入 tailnet(讓上行加密,並讓伺服器 1883 連得到)
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --authkey=tskey-auth-XXXX --advertise-tags=tag:robot

# 2. 裝套件(純 Python,ARM 上不用編譯)
pip install itri-fleet-agent
```

唯一的相依是 `paho-mqtt`。登記走標準函式庫的 `urllib`,樹莓派上不需要再裝 wheel。
支援 Python 3.9 以上(Raspberry Pi OS Bullseye 內建 3.9 就能跑)。

## 三步驟上線

```bash
# 1. 用管理頁產生的一次性 token 換取本車憑證
itri-agent enroll --server https://<你的節點>.<你的tailnet>.ts.net --token XXXX-XXXX-XXXX

# 2. 掃描本地 topic,即時跳動,依編號選擇
itri-agent discover

# 3. 開始轉發
itri-agent run
```

### `discover` 長這樣

```
   #  TOPIC                            Hz   筆數   位元組  最新值
  ────────────────────────────────────────────────────────────────
●  1  chassis/bat_pct                 0.5      4       5  100.0
●  2  chassis/bat_voltage             0.5      4       5  50.0
   3  chassis/estop                   0.2      2       5  False
●  4  chassis/mode                    0.2      2       7  moving
●  5  chassis/motor/1/current_a       4.8    114       5  4.448
  ...
  19 個 topic · 346 筆 · 40.5 筆/秒 · 已掃描 9s

輸入要轉發的編號,例如 1,3,5-8;全部輸入 all
> 1,2,4,5-9,11
```

`●` 表示這個 topic 剛剛才跳動過。選完會**先估算成本**再寫入設定:

```
預估上傳 23.8 筆/秒 → 0.35 GB/天 → 10.5 GB/月(單台車)
```

## 設定檔

`~/.itri-fleet/config.json`(憑證分開存在 `credentials.json`,權限 0600)

| 欄位 | 預設 | 說明 |
|---|---|---|
| `local.host` / `local.port` | `127.0.0.1:1883` | 上位機自己的 broker |
| `include` | `discover` 選的清單 | 空的代表全部轉發 |
| `exclude` | `fleet/#`、`$SYS/#` | 永遠不轉(避免自己轉自己) |
| `max_rate_hz` | `5.0` | **每個 topic** 的上限 |
| `on_change_only` | `true` | 值沒變就不送 |
| `deadband` | `0.0` | 數值變化小於此值視為沒變 |
| `max_payload_bytes` | `8192` | 超過就丟(擋掉影像 / 點雲) |
| `publish_hz` | `1.0` | 上行批次頻率 |
| `buffer_max` | `200000` | 上行斷線時本機緩衝的筆數 |
| `map` | `{}` | 選配:對應到儀表板卡片欄位 |

### 流量控制很重要

`on_change_only` + `max_rate_hz` 是預設開啟的,實測可以砍掉一大半:

```
收 857 筆 → 轉 502 筆   (略過 頻率 104 / 重複 245)
```

單台車 19 個 topic 大約是 **9–10 GB/月**。50 台就是 **450–500 GB/月**,
所以磁碟空間要先算清楚,或把 `max_rate_hz` 調低、把高頻 topic 排除。
`discover` 選完就會告訴你數字,不用等到塞爆才發現。

## 對應到儀表板卡片(選配)

不設定也能用 —— 資料照樣完整入庫,只是儀表板上只看得到「上線 / 離線」。
要讓電量、狀態、速度的卡片亮起來:

```json
"map": {
  "battery": "chassis/bat_pct",
  "state":   "chassis/mode",
  "v":       "chassis/vel/linear",
  "w":       "chassis/vel/angular",
  "temp":    "chassis/motor/1/temp_c",
  "odom":    "chassis/odom_m"
}
```

同型號底盤的第 2 台之後直接複製這段就好。

## 開機自動啟動

```bash
itri-agent install-service     # 印出 systemd unit 與安裝指令
sudo systemctl enable --now itri-agent
journalctl -u itri-agent -f
```

## 斷線會怎樣

| 情況 | 行為 |
|---|---|
| 上行斷線 | 資料存進本機記憶體緩衝,最多 20 萬筆,恢復後**照原始時間戳**補傳 |
| 本地 broker 斷線 | paho 自動重連(退避 1→30 秒) |
| 憑證被撤銷 | CONNACK 135,log 會告訴你要重新 `enroll` |
| 緩衝滿了 | 丟最舊的並計數,不會靜默掉資料 |

## 其他指令

```bash
itri-agent status        # 目前設定與憑證
itri-agent discover -y   # 不問直接全選
itri-agent discover --host 192.168.1.50 --port 1883   # broker 不在本機
```
