# ITRI Fleet Server

多台輪式機器人的即時監控與長期記錄伺服器。
機器人上位機把自己的 MQTT topic 原樣轉發上來,伺服器彙整成網頁儀表板,
並透過 Tailscale Funnel 用一個**固定的公開網址**對外,不需要買網域、固定 IP、
port forwarding 或憑證。

```
   車輛上位機 ×N                     伺服器                          外部
 ┌──────────────────┐        ┌──────────────────────────┐
 │ 底盤程式          │        │  內建 MQTT broker :1883   │
 │   ↓ 自己的 topic  │        │    每車獨立帳密 + ACL      │
 │ 本地 broker      │  MQTT  │         ↓                │
 │   ↓              │───────►│  即時狀態(記憶體)         │
 │ itri-agent 訂閱 # │Wireguard│    ↓         ↓           │
 └──────────────────┘        │ PostgreSQL  預警引擎       │
                             │    ↓         ↓           │
                             │  FastAPI + WebSocket :8080│
                             └───────────┬──────────────┘
                                         │ Tailscale Funnel
                                         ▼
                        https://<你的節點>.<tailnet>.ts.net
                                         │
                                  瀏覽器 / 手機 PWA(可推播)
```

---

## 一、安裝(全新機器)

### 需要準備

| 項目 | 說明 |
|---|---|
| Windows 10/11 或 Linux | 主要在 Windows 驗證 |
| Python 3.9+ | |
| Tailscale 帳號 | 免費 Personal 方案即可 |
| 磁碟空間 | 50 台車保留一個月約 **40 GB** |

**不需要**:網域、固定 IP、TLS 憑證、信用卡、系統管理員權限。

### 三行搞定

```bat
git clone https://github.com/Kuohsuanyu/itri_fleet_server.git
cd itri_fleet_server
python tools\setup.py
```

`setup.py` 是互動精靈,會一步步帶你走完:

| 步驟 | 做什麼 |
|---|---|
| 1 | 檢查 Python 與相依套件,缺的話問你要不要裝 |
| 2 | 找 PostgreSQL;沒有的話可下載**免安裝版**(不需要管理員權限) |
| 3 | **問你資料要存哪個目錄**,initdb 並套用效能設定 |
| 4 | 建立資料庫 |
| 5 | 產生 Web Push 的 VAPID 金鑰 |
| 6 | **設定儀表板密碼**(留空會產生 24 字元隨機密碼) |
| 7 | 檢查 Tailscale,指引開通 Funnel |
| 8 | 建立資料表、啟動伺服器驗證 |

每一步都能中斷,重跑會接續而不是從頭來。

跑完會產生 **`config.yaml`** —— 所有機密和可調設定都在裡面,而且它在 `.gitignore` 裡,
永遠不會被 commit。

### 開通外網(一次性)

`setup.py` 之後執行:

```bat
tailscale funnel --bg 8080
```

第一次會印出兩個授權連結(Serve 和 Funnel),點進去 Approve。
另外要到 https://login.tailscale.com/admin/dns 開啟 **HTTPS Certificates**。

公開網址就是這台機器的 Tailscale 節點名稱。要換名字:

```bat
tailscale set --hostname=itri
```

⚠️ 節點名稱決定網址,而且**一個 tailnet 內不能重複**。搬機時舊機器要先讓出名字。

---

## 二、日常操作:控制台

```bat
scripts\0_控制台.bat
```

```
  ITRI Fleet 控制台   2026-08-09 08:08:58
  ────────────────────────────────────────────────────────────
   PostgreSQL        ● 執行中         127.0.0.1:5432
   Fleet Server      ● 執行中         PID [11444]  :8080 開
   MQTT broker       ● 執行中         port 1883 · bind=tailscale
   Tailscale Funnel  ● 已發佈         https://<你的節點>.ts.net
   外網可達          ● HTTP 200       任何人都能開

   模擬底盤          ● 執行中         PID [25008]
   itri-agent        ● 執行中         PID [17024]
  ────────────────────────────────────────────────────────────
   車隊     1/2 在線   訊息 2,335   異常 0   平均電量 96.9%
   資料庫   連線中   遙測 2,170 列   topic 57,874 列   零丟失
   MQTT     連線 9   認證失敗 0   ACL 拒絕 0
   外網流量 0.0 KB/s   推估 0.07 GB/月   分頁 0
   告警     觸發 139  恢復 139

   1 啟動全部  2 停止全部  3 資料庫  4 伺服器  5 外網
   6 模擬環境  7 清除重複  8 開啟儀表板  9 log  t 測試
   d pgAdmin  s 快速 SQL  w 持續監看  q 離開
```

**「外網可達」是真的去打公開網址**,不是讀設定檔猜的 —— Funnel 顯示 on
但伺服器掛了會分辨得出來。

關掉控制台視窗**不會**關掉任何服務,它們是獨立行程。

其他模式:

```bat
python tools\console.py --status          印一次就結束,適合排程
python tools\console.py --watch           持續刷新
python tools\console.py --action funnel-off
```

### 個別腳本

| 腳本 | 用途 |
|---|---|
| `0_控制台.bat` | 平常只要這一個 |
| `1_啟動伺服器.bat` | MQTT broker + 網頁伺服器 |
| `2_模擬車隊.bat` | 註冊 12 台假機器人 |
| `2b_模擬底盤上位機.bat` | 模擬廠商底盤(19 個 topic)到本地 broker |
| `3/4_開啟/關閉外網.bat` | Funnel |
| `5_流量測試.bat` | 量 Funnel 頻寬 |
| `6/7_啟動/停止資料庫.bat` | PostgreSQL |
| `8_安全性測試.bat` | 端對端 36 項驗證 |
| `9_資料庫介面.bat` | 開 pgAdmin |

---

## 三、新增一台車

### 伺服器端

開 `https://<你的網址>/admin/robots` → **「+ 新增車輛」** → 填名稱 →
拿到一次性授權碼(預設 30 分鐘)和一段可複製的指令。

### 車輛上位機(樹莓派等)

**先讓它自己判斷該怎麼裝** —— 底盤走 ROS 2 和走本地 MQTT broker 的裝法不一樣:

```bash
# 1. 加入 tailnet(broker 只聽 Tailscale 介面,這步不能跳)
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --authkey=tskey-auth-XXXXX --advertise-tags=tag:robot

# 2. 掃描這台機器(純讀取,不改任何東西)
sudo apt install -y pipx && pipx ensurepath
pipx install https://<你的網址>/agent/latest.whl
itri-agent doctor
```

`doctor` 會回報 Python、ROS 2、本地 broker、Tailscale、跟伺服器的連線,
然後給一組指令,並明確列出**會改動什麼**和**不會碰什麼**。照著它給的走即可。

<details>
<summary>兩種資料來源的差別</summary>

| | 走本地 MQTT broker | 走 ROS 2 |
|---|---|---|
| 裝法 | pipx(完全隔離) | venv `--system-site-packages` |
| 為什麼 | 只需要 paho-mqtt | rclpy 不在 PyPI,是 apt 裝在 `/opt/ros` |
| 執行前 | 不用做什麼 | 每次都要 `source /opt/ros/$ROS_DISTRO/setup.bash` |

ROS 那個 source **不是裝一次就好**:rclpy 靠 `setup.bash` 匯出的 `PYTHONPATH`
才找得到,它的 `.so` 也要 `LD_LIBRARY_PATH`。忘了 source 的典型症狀是
「在我的 shell 跑得動,systemd 開機起不來」。`itri-agent install-service`
在 ROS 模式下產生的 unit 會自己 source,所以照著做不會踩到。

ROS 模式的三個坑已經處理:訂閱前先比對發佈端 QoS(感測器多半 BEST_EFFORT,
用預設 RELIABLE 訂會**靜默收不到**)、大陣列只記長度(`/scan` 的 ranges
不會變成幾百筆)、Image/PointCloud2/OccupancyGrid/TF 預設略過。

</details>

```bash
# 3. 設定精靈:問你伺服器網址和授權碼,然後掃描 topic 讓你勾選
itri-agent

# 4. 開機自動啟動(重要,見下)
itri-agent install-service
sudo systemctl enable --now itri-agent
```

第 3 步的 `discover` 會列出**所有**的 topic(ROS 或 MQTT 都一樣),
即時跳動、依編號選取,並在選完後告訴你預估流量。
**agent 不解析任何欄位** —— 換底盤廠牌不用改程式碼。

詳見 [itri_fleet_agent](https://github.com/Kuohsuanyu/itri_fleet_agent)。

### 公開與私有:兩個 port

Funnel 把 HTTP 服務推上公開網際網路。所以只有儀表板放在那裡:

| | port | 綁在 | 內容 |
|---|---|---|---|
| 公開 | 8080 | 0.0.0.0 | 儀表板、WebSocket、登入 |
| 私有 | 8081 | Tailscale 位址 | 管理、登記、agent wheel |

公開 port 上 `/admin`、`/api/admin`、`/api/enroll`、`/agent/` 一律回 **404**
—— 不是 401。公開的呼叫者連「這裡有管理介面」都不該知道。

判斷依據是連線落在哪個 port,不是 header —— header 客戶端可以偽造,
監聽的 port 不行。

`/api/enroll` 特別需要這樣:它**照設計就不需要認證**(一次性授權碼本身就是
憑證)。放在公開網路上等於讓全世界來猜授權碼。而車輛在登記前**早就已經在
tailnet 上了**,根本不需要走公開路徑。

```bash
tailscale funnel status     # 必須指向 8080
```

管理頁的網址是 `http://<你的tailnet位址>:8081/admin/robots`,
或用 MagicDNS `http://itri:8081/admin/robots`。

把 `http.private_port` 設成 `null` 會退回單一 port(舊行為),啟動時會警告。

### Tailscale ACL —— 記得套用

`docs/tailscale-acl.hujson` 把車輛限制成**只能連伺服器的 1883**。
車連不到車、連不到 PostgreSQL、連不到管理介面、SSH 不進伺服器。

Tailscale 的預設政策是「任何裝置連任何裝置的任何 port」,對車隊太寬 ——
車輛是最容易被實體接觸的,拿下一台就等於拿到整個 tailnet。

**這要你手動貼到後台**(Access Controls),程式碼改不了它。
政策檔裡附了 tests 區塊,按 Save 時會自動驗證「不該通的有沒有真的不通」。
套用前先按 Preview,貼錯會把自己鎖在外面。

### 授權碼只用一次

| 情況 | 要重新認證? | 資料缺口 |
|---|---|---|
| 網路斷、伺服器重啟 | 不用,自動重連 | **0 秒**(agent 緩衝) |
| 資料庫掛掉 | 不用 | **0 秒**(伺服器緩衝) |
| 車子重開機 | 不用,讀本機憑證 | 停機那段 |
| **agent 行程死掉** | 不用 | **等於死掉的時間** |
| 管理員按「撤銷」 | 要,重發授權碼 | — |

實測:伺服器停 21 秒 → 零缺口;資料庫停 25 秒 → 零缺口;
agent 自己死 10 秒 → **缺 10.13 秒**。

最後一列沒有任何機制能救:緩衝在 agent 裡面,agent 死了緩衝也死了。
`itri-agent install-service`(Restart=always,RestartSec=10)把缺口壓到
重啟時間,但**不是零**。所以它不是可選項。

### 重複資料

MQTT QoS 1 是 at-least-once。重連時整批會被重送,而重送的列**看起來
跟真的一模一樣** —— 之後任何 count、avg、速率計算都是錯的,而且不會有
人發現。

agent 每批帶 `boot_id` + `seq`,伺服器用 `(robot_id, boot, seq)` 去重,
在寫進資料庫**之前**擋掉。`/api/metrics` 的 `mqtt.duplicate_batches`
會告訴你擋了多少 —— 這個數字一直爬代表上行在抖。

`unversioned_batches` 是還沒升級的舊 agent 送的,那些**無法去重**。

---

## 四、設定:`config.yaml`

所有會想調整的東西都在這個檔,改完重啟伺服器即可,不用動程式碼。
範本是 `config.example.yaml`。

| 區段 | 常用項目 |
|---|---|
| `http` | `password` 儀表板密碼、`port`、`session_days`、登入失敗限流 |
| `mqtt` | `bind`(建議 `tailscale`)、`public_host`、`require_auth`、topic 命名 |
| `dashboard` | `push_hz`(**最主要的流量旋鈕**)、`offline_after`、`history_points` |
| `registry` | `token_ttl_min` 授權碼有效期、憑證快取 |
| `database` | `dsn`、`retention_days`、緩衝上限、分區預建天數 |
| `alerts.channels` | Web Push / ntfy / Telegram / webhook / LINE / email 的憑證 |
| `bwtest` | 頻寬測試端點(**預設關閉**) |

### 兩個為了可攜性的設計

```yaml
mqtt:
  bind: tailscale                        # 啟動時自動偵測本機 Tailscale IP
  public_host: <你的節點>.<tailnet>.ts.net  # MagicDNS 名稱,不是 IP
```

`public_host` 用 MagicDNS 名稱而不是 `100.x` 位址很重要:已登記的車會把它寫進
`credentials.json`,寫死 IP 的話搬機後整隊連不上。

### 預警規則不在這裡

門檻、範圍、通知內容都在網頁上設(`/admin/alerts`),存在資料庫,**改門檻不用重啟**。
`config.yaml` 只放通知管道的憑證。

---

## 五、儀表板

| 路徑 | 內容 |
|---|---|
| `/` | 即時監控:卡片、篩選、電量趨勢、告警橫幅 |
| `/admin/robots` | 車輛註冊、發授權碼、撤銷 |
| `/admin/topics` | **Topic 瀏覽器**:每台車實際送過什麼、趨勢圖、一鍵建規則 |
| `/admin/alerts` | 預警規則、觸發中、告警歷史 |
| `/admin/events` | 事件稽核軌跡 |
| `/admin/system` | 資料庫用量、推播裝置、外網流量 |

### 手機:PWA + 推播

儀表板是 PWA,可以「加入主畫面」變成 App,並接收 Web Push 通知 ——
**不用上架、不用 Apple Developer 帳號**(iOS 16.4+ 支援 home-screen web app 推播)。

進站幾秒後會問要不要開啟通知;右上角的 🔔 隨時可切換。
手機版導覽收在漢堡選單裡,右上角只留通知按鈕。

⚠️ **LINE Notify 已於 2025-03-31 終止服務。** 替代的 Messaging API
每月只有 200 則免費,建議只給 critical 用。

---

## 六、安全性

### 三層獨立防護

```
1. 網路層   Tailscale ACL      tag:robot 只能連 broker:1883
2. 傳輸層   WireGuard          免費附贈的加密
3. 應用層   MQTT 帳密 + ACL    每車只能碰 fleet/<自己的 id>/*
```

攻擊者需要**同時**取得 tailnet 存取權和 MQTT 憑證。就算兩層都破,
topic ACL 讓傷害範圍限制在那一台車。

⚠️ `mqtt.bind` 是安全設定,不是方便設定。綁 `0.0.0.0` 的話區網任何人都能繞過
Tailscale 直接連 1883 —— 那一層等於不存在。用 `tailscale` 讓它只聽 WireGuard 介面。

### 登入

儀表板未登入一律導向 `/login`;API 回 401;WebSocket 拒絕。
密碼不進 cookie —— 登入後發一組隨機 session id,`HttpOnly` + `Secure`。
同一 IP 5 分鐘內失敗 10 次回 429。

### fail-closed

`require_auth: true` 但資料庫冷啟動連不上時,**所有車都會被拒絕,而且伺服器
拒絕啟動 broker**,不會退回無認證模式。所以要先啟動資料庫再啟動伺服器
(控制台的「1 啟動全部」順序正確)。

### 驗證

```bat
scripts\8_安全性測試.bat
```

36 項端對端檢查:token 重用防護、亂猜防護、冒充他車、萬用字元訂閱、
撤銷立即踢線、舊密鑰失效。

---

## 七、資料儲存

PostgreSQL,**全保真不降頻**,按天分區,過期分區用 `DROP TABLE` 清除
(瞬間完成,不留 bloat)。

| 資料表 | 內容 | 保留 |
|---|---|---|
| `robots` / `enroll_tokens` | 註冊與憑證雜湊 | 永久 |
| `telemetry` | 標準欄位遙測,按天分區 | `retention_days`(預設 31) |
| `topic_samples` | agent 轉發的原始 topic,按天分區 | 同上 |
| `topic_catalog` | 每台車送過哪些 topic | 永久 |
| `events` | 狀態轉換、錯誤、上下線、登記、告警 | **永久** |
| `alerts` / `alert_rules` | 告警紀錄與規則 | 永久 |

遙測和事件分開是刻意的:遙測量大可過期,事件是稽核軌跡、量小、必須永久保存。

### 實測容量

| 項目 | 實測 |
|---|---|
| 每列(含索引) | **162.5 bytes** |
| 50 台 @ 2 Hz | 1.25 GB/天,**約 40 GB/月** |
| agent 轉發 19 個 topic | 單台約 10 GB/月,50 台約 500 GB/月 |

轉發全部 topic 很貴 —— `on_change_only` + `max_rate_hz` 預設開啟,
實測可砍掉近四成,`discover` 選完會先告訴你數字。

### 資料庫掛掉不影響監控

| 情境 | 行為 |
|---|---|
| 儀表板 | 照常(即時狀態在記憶體,不查資料庫) |
| 歷史 API | 回 503,誠實地說查不了 |
| 寫入 | 緩衝在記憶體,DB 回來自動補寫 |
| 資料缺口 | **0 秒**(補寫帶原始時間戳) |

實測強制 `pg_ctl -m immediate stop` 25 秒:緩衝 2600 筆,零丟失,零缺口。

### 圖形介面

```bat
scripts\9_資料庫介面.bat        開 pgAdmin 4
scripts\0_控制台.bat  → s       快速 SQL(六個預設查詢)
```

`tools/queries.sql` 有十組現成查詢(事後查錯、資料連續性檢查等),
pgAdmin 裡 File → Open File 選它,游標放在某段按 F5。

---

## 八、備份與搬機

```bat
python tools\backup.py export --no-history   搬機用,約 0.1 MB
python tools\backup.py export                完整,含歷史
python tools\backup.py restore <bundle.zip>
```

包含 `config.yaml`、資料庫 dump、agent wheel。
**不包含 Tailscale 身分** —— 節點金鑰不能複製,新機器要自己 `tailscale up`。

### 搬到新機器

```bat
:: 舊機器
python tools\backup.py export --no-history
scripts\4_關閉外網.bat
tailscale set --hostname=itri-old         :: 讓出名字,否則新機器會變 itri-1

:: 新機器
git clone ... && python tools\setup.py
tailscale up --hostname=itri              :: 網址原封不動
python tools\backup.py restore <bundle.zip>
scripts\0_控制台.bat → 1 啟動全部
```

**已登記的車不用重新登記** —— 憑證雜湊在資料庫裡跟著搬,MagicDNS 名稱不變。

⚠️ 備份檔含 VAPID 私鑰、儀表板密碼、資料庫密碼,當機密保管。`backups/` 已在 `.gitignore`。

---

## 九、開發

```
itri_fleet_server/
├─ config.example.yaml       設定範本(config.yaml 被 gitignore)
├─ server/
│  ├─ main.py                FastAPI、WebSocket、登入、路由
│  ├─ broker.py              內建 MQTT 3.1.1 broker(認證 + topic ACL)
│  ├─ ingest.py              MQTT → 即時狀態 + 歸檔
│  ├─ state.py               記憶體中的車隊狀態
│  ├─ db.py / history.py     PostgreSQL 連線池、分區、緩衝寫入
│  ├─ registry.py            車輛註冊、一次性授權碼、憑證
│  ├─ alerts.py              預警引擎 + 6 種通知管道
│  ├─ metrics.py             外網 egress 計量
│  └─ schema.sql             資料表定義(冪等)
├─ web/                      儀表板 + 五個管理分頁 + PWA
├─ agent/                    車輛端套件原始碼(另有獨立 repo)
├─ tools/
│  ├─ setup.py               安裝精靈
│  ├─ console.py             控制台
│  ├─ backup.py              備份 / 還原
│  ├─ scan_secrets.py        推送前掃金鑰
│  ├─ check_package.py       確認 agent 套件不夾帶伺服器資料
│  ├─ sim_*.py               模擬車隊與底盤
│  └─ test_*.py              端對端測試
└─ scripts/                  0~9 一鍵批次檔(純 ASCII)
```

### 推送前必跑

```bat
python tools\scan_secrets.py
```

掃 git 會提交的檔案裡有沒有金鑰、tailnet 名稱、真實 token、本機路徑。
`.gitignore` 擋得住「沒被加進去的檔案」,擋不住「寫進程式碼或說明裡的金鑰」。

⚠️ `.gitignore` **不支援行尾註解** —— `config.yaml  # 說明` 整行會被當成字面樣式,
永遠匹配不到。註解必須自成一行。這個坑差點讓 VAPID 私鑰被 commit。

---

## 十、已知限制

| 項目 | 狀況 |
|---|---|
| **真實樹莓派** | **未實測** —— 只在 Windows 乾淨 venv 驗證過 |
| **真實 Tailscale tag:robot 加入** | 未實測 |
| **systemd 服務** | 只產生設定檔,未實際安裝過 |
| **真實底盤** | 全部是模擬的 |
| 開機自動啟動(伺服器端) | 未做 —— 重開機後要手動啟動 |
| 磁碟空間保護 | 未做 —— 塞滿後 PostgreSQL 停止寫入,無預警 |
| MQTT over TLS | 未做 —— 靠 Tailscale 的 WireGuard 加密 |

### 環境相關的坑(都已處理,記錄備查)

- **Windows 的 cmd.exe 用系統編碼解析 .bat**,UTF-8 中文會被誤讀成命令分隔符。
  所有 `.bat` 因此保持純 ASCII,中文輸出交給 Python。
- **psycopg 的 async 模式不支援 Windows 預設的 ProactorEventLoop**,
  所以用同步驅動跑在 `asyncio.to_thread`(兩平台行為一致)。
- **cp950 主控台下 psql 不能用中文欄位別名**,會回
  `invalid byte sequence for encoding "UTF8"`。控制台的快速查詢走 psycopg,沒這問題。
- 這台機器若已有 Mosquitto 佔用 `127.0.0.1:1883`,伺服器會偵測並警告;
  它的 ingest 走行程內直送,不受影響。
