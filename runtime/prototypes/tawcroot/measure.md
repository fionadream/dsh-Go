# 5042 / 5052：三方式隔离测量【原型，不合入】

未真机验证。作者没有运行 Android、JVM、NDK、Gradle 或 Flutter；下面是交给 owner 在一次性模拟器上执行的入口，不是已通过的测量结果。5040 的构建说明见同目录 `README.md`，5041 提供纯参数映射器。本原型不单独请求代跑。

## 范围与前置条件

测量分两包交付：5042 提供 debug Activity、结果 schema 和本文；5052 提供宿主脚本、guest fixture 与轻量测试。以原始基底为根，先应用 **5018→5019→5040 r2→5041→5042→5052**，两包齐备后才运行下列命令。首包可独立编译，但不是完整测量入口；生产运行时的其余修复以 owner 选择的候选基底为准。它不改 targetSdk、minSdk、默认启动方式或正常安装流程，也不调用 `MainActivity`、`RuntimeController`、`RuntimeInstaller`。原型运行门为 API ≥29；实际 `actualTarget` 写入每条结果，target 28 的结果不能当成 target 36 兼容证据。

使用一个只为本实验创建、由单个测量进程独占的模拟器和全新测试安装。不要先打开正常 App、创建正常 Linux 环境。脚本不安装、覆盖、卸载或清除 App；每次 `am force-stop` **之前**都用 `run-as` 检查 `files/runtime`、`files/runtime.reset`、`files/runtime-keep` 不存在。发现任一目录即拒绝。它不接收用户 rootfs 路径，也不读取这些目录的内容；该门不能代替 owner 对“一次性测试安装”的确认。

guest 由独立 debug Activity 在普通 App UID/SELinux 域中通过 `ProcessBuilder` 启动。`adb shell` 只调用 Activity；`run-as` 只传入本轮配置、fixture、Git 归档及读取结果，不用它运行 guest。Activity 仅存在于 `src/debug`，入口要求 `android.permission.DUMP`。AOSP Android 14 的 [平台声明](https://android.googlesource.com/platform/frameworks/base/+/android-14.0.0_r50/core/res/AndroidManifest.xml)将它设为 signature/privileged/development，[Shell 声明](https://android.googlesource.com/platform/frameworks/base/+/android-14.0.0_r50/packages/Shell/AndroidManifest.xml)请求该权限；它并非“仅 shell”，其他持权调用者也可能访问。实际模拟器的权限和入口可达性尚待验证。

## 构建与安装

在仓库根目录执行。需已有项目要求的 Flutter/SDK、NDK `28.2.13676358`、Python 3.11+ 和 adb；测量脚本不自动代办这些步骤。先通过已有流程准备所选 ABI 的普通 runtime 资产；如果已有匹配产物，可复用，不能混用不同 ABI。

```sh
# x86_64 模拟器示例；既有运行时构建不计入测量。
bash runtime/build-runtime.sh x86_64

python3 -I runtime/prototypes/tawcroot/build.py \
  --ndk "$ANDROID_HOME/ndk/28.2.13676358" \
  --archive /absolute/path/tawc-a49a4075228f4801dadc1eee584aed33fe8ea4ce.zip \
  --offline

DSH_TAWCROOT_PROTOTYPE=1 bash tooling/build-apk.sh --debug -f x64

# 仅安装到没有该 App 的全新一次性模拟器；不用 -r 覆盖既有安装。
adb -s emulator-5554 install app/build/app/outputs/flutter-apk/app-x64-debug.apk
```

arm64 对应 `build-runtime.sh arm64-v8a`、`build-apk.sh --debug -f arm64` 和 `app-arm64-debug.apk`。5040 的 OUT 固定为 `runtime/.work/tawcroot-prototype/out`，含两 ABI 的 launcher。普通 debug/profile/release 不自动打入该 launcher；必须显式打开 5040 的 debug 构建开关。不要发布这个 APK。

脚本先核对本机 APK 内的 `assets/tawcroot-prototype/manifest.json` 与 OUT 根清单逐字一致，确认固定 commit、源码 ZIP SHA、NDK、所选 ABI、JNI 字节数/SHA 和许可条目。Activity 随后独立核对实际安装的 APK、运行时清单、提取后的 tawcroot 及四个既有 PRoot JNI 文件；不一致直接失败。`--apk` 必须指实际安装的那个文件，不支持 split APK。

## 可直接运行的命令

所有 `--out` 都必须是尚不存在的新目录。默认仅接受 `ro.kernel.qemu=1`；`--allow-device` 只是 owner 显式选择一次性真机测试安装的开关，不允许使用真实用户数据。

先运行兼容门：

```sh
python3 -I runtime/prototypes/tawcroot/measure.py \
  --serial emulator-5554 \
  --apk app/build/app/outputs/flutter-apk/app-x64-debug.apk \
  --out /absolute/path/results/gates-01 \
  --preflight-only
```

`--preflight-only` **会实际执行**每种方式的 Node、PTY、stop 三个隔离门，只是不做性能场景；不是只读 APK 检查。任一方式的门失败，跳过该方式后续场景。停止无法确认、宿主传输异常或清理失败时保留证据并停止本次后续尝试，不继续 `force-stop` 或换方式接手该树。

只验证整条链的短运行：

```sh
python3 -I runtime/prototypes/tawcroot/measure.py \
  --serial emulator-5554 \
  --apk app/build/app/outputs/flutter-apk/app-x64-debug.apk \
  --out /absolute/path/results/startup-smoke-01 \
  --suites startup --rounds 1 --smoke
```

正式比较需同一 ABI、同一 APK、同一固定 Git 副本，至少 10 个配对块。准备一份**无凭据、无用户私人内容、由 owner 信任其配置和 hooks 的离线仓库副本**，目录名为 `repo/`，包含完整 `.git/HEAD`；不接受 worktree 的 `.git` 指针。可在副本中加入有明确定义的未跟踪/脏文件，再记录实验条件。例：

```sh
PROTO_FIXTURE_DIR=$(mktemp -d)
git clone --no-local /absolute/path/trusted-benchmark-repo "$PROTO_FIXTURE_DIR/repo"
PROTO_GIT_COMMIT=$(git -C "$PROTO_FIXTURE_DIR/repo" rev-parse HEAD)
git -C "$PROTO_FIXTURE_DIR/repo" checkout --detach "$PROTO_GIT_COMMIT"
tar -C "$PROTO_FIXTURE_DIR" -cf "$PROTO_FIXTURE_DIR/repo.tar" repo

python3 -I runtime/prototypes/tawcroot/measure.py \
  --serial emulator-5554 \
  --apk app/build/app/outputs/flutter-apk/app-x64-debug.apk \
  --out /absolute/path/results/full-cold-01 \
  --suites startup,pnpm,apt,git --rounds 10 \
  --pnpm-cache cold \
  --git-archive "$PROTO_FIXTURE_DIR/repo.tar" \
  --git-commit "$PROTO_GIT_COMMIT"
```

归档必须是未压缩 tar，条目都在 `repo/` 下，只允许普通文件、目录和留在树内的相对 symlink；不接受硬链接条目、设备或越界路径。输入是显式提供的实验副本，不从手机导出用户仓库。传输时宿主将 Git 归档读入内存，需为代表性规模保留足够宿主内存、模拟器磁盘和时间。

默认 3 模式×4 场景×10 轮，加 9 个兼容门，共计划 129 个独立 unit。每个都重新解包 APK 运行时，完整实验可能耗时很长；只将相应运行段计入场景耗时。`--timeout` 是每个 prepare/run 阶段的宿主上限，默认 600 秒，范围 10–3600 秒；同一实验预先固定同一上限。比较 pnpm 热 store 时另用新 `--out` 和 `--pnpm-cache warm` 完整运行，不能混进冷数据。

## 一次性 unit 与执行方式

每次尝试新建 UUID，目录固定为 App 私有的 `files/tawcroot-prototype/<uuid>/`，包括 `.owned` 标记、只允许启动一次的 `.run-started`、`rootfs/`、同级 `tawcroot/` 硬链接 store、`dev-view/`、私有 `storage/`、PRoot 临时目录、显式传入的 fixture 和本轮结果。schema 1 解包整包；schema 2 按 base→engine 解包，每个归档核对压缩大小/SHA。既有 `TarExtractor` 的权限、symlink 和硬链接转副本规则保持不变。

prepare 和 run 分别从强停后的 App 进程启动。prepare 做本轮复制与必要的场景准备，run 使用同一 unit 的准备态。运行过 tawcroot 的树绝不回落给 PRoot。四个 file bind 经 5041 的 dev-view 计划准备；`/sdcard`、`/storage` 都绑定本轮私有空目录，不访问手机共享存储。

| 请求方式 | 实际接线与报告边界 |
| --- | --- |
| `proot` | 原运行时清单与既有 PRoot 参数展开，只替换一次性根、临时/存储目录和固定探针命令。 |
| `direct` | 先用合法 web 模板调用既有 `DirectLaunch.prepare` 生成配置/wrapper，再对非 web 场景运行显式 Node fixture，移除 `DSH_GO_DIRECT_MAIN`。Node 使用现有 glibc loader/preload；其他工具仍可能经已有 adapter/wrapper 使用 PRoot，结果明确记录该混合执行器。 |
| `tawcroot` | 只调用 5041 的纯 mapper，执行已核身份的静态 launcher；pipe 父进程传 `pty=false`。fixture 内的 node-pty 创建 PTY/session，Direct 分支显式调用既有 adapter.command。 |

`actualExecutor` 只在 `ProcessBuilder.start()` 成功后填写，是外层启动计划与 exec 成功的分类；fixture 另报 Node/子命令适配路径，**不是逐 syscall 跟踪**。Direct 不提供通用 fs 路径重写，fixture 的文件读写显式用已有 `adapter.toHost`，没有增加生产 fs shim。

## 兼容门与场景判定

| 门或场景 | 实际检查什么 | 明确没有证明什么 |
| --- | --- | --- |
| Node | Worker 与主线程 localhost DNS；固定 Wasm 导出 `add(37,5)` 返回 42；已有 sharp 生成 2×2 PNG；link/symlink/rename 内容；随机设备、child 自己的标准流和 `/dev/fd`；同 UID 抽象 AF_UNIX 的本地合成 token 握手。 | 一次小 Wasm 调用不覆盖长期 JIT/高 tier；不覆盖所有 native addon、网络切换或长期 vfork 压力。硬链接后写入可见性和 nlink 是记录项，语义取舍由 owner 决定。 |
| PTY | 交互 bash、resize、前台命令 Ctrl-C、后台 job、Ctrl-D 正常退出；独立第二个 PTY 请求 TERM 并观察退出，提前退出或需 KILL 失败。 | 不证明所有 shell 信号组合；销毁项记录请求与退出观察。 |
| stop | Node 启动 FIFO child，收到分片安全的固定短标记后，App 按已识别身份请求停止，报告残留/不可观测数。 | 标记只说明已走到 FIFO 打开尝试；`blockedSyscallProven=false`，没有观察内核阻塞点或所有阻塞 syscall。 |
| startup | web token 到达且 loopback 端口可连接，随后正常停止；提前观察到进程退出、KILL 或残留均失败。 | 不测生产 Controller ready、Flutter 首屏或完整 HTTP 业务就绪。 |
| pnpm | 固定 `dsh-git-badge@0.20.1` 的 add 主耗时；核 profile 依赖版本、bundles 数组成员及模块，remove 后复核，再核 web 的 token/端口与正常停止。 | 仍在线取包，不称离线闭包。cold 是本轮未额外预热，不代表 APK 无预置缓存或页缓存已清；warm 先执行一次 add/remove。 |
| apt | prepare 冻结 `hello` 版本并 download-only，跨轮比较版本/缓存内容摘要；run `--no-download` install，核 dpkg 状态、空 audit、hello 执行、remove 与后续取锁。 | 不覆盖中断 dpkg 事务恢复；仓库失效或缓存不同明确失败，不临时改版本。 |
| Git | 固定 commit/归档，记录 tracked/untracked 数和工作树摘要；首次 status、热 status、关 fsmonitor/untrackedCache 的归因遍；核输出一致、fsck、工作树未变，跨方式/轮次保持首次成功输出参考。 | prepare 的 `ls-files` 已读 index，`firstStatusPass` 只指首次 status，不是冷 index 或冷页缓存。 |

web token 只在进程内判断就绪或做本地握手，不写入 result、stdout、诊断日志或证据。普通 guest stdout/stderr 只消费、不保存原文；子命令报告字节数/SHA。主命令非零、超时或流未结束不能被后续成功检查覆盖。pnpm 的 restart 保留实际 `exitCode/signal`、TERM 请求及是否提前观察到退出；只接受请求停止后观察到 code 0 或 SIGTERM，其余失败。

## 时钟、统计和磁盘口径

`clocks` 在同一设备 `android.elapsedRealtimeNanos` 域中。`processStartNs` 由 Android 的毫秒级进程启动时间换算，不能据 ns 字段声称纳秒精度。宿主 adb 的 `amStartTotalTimeMs` 和 Node 的 hrtime 另属独立时钟，只比较各自持续时间，不跨域相减。

| 分段 | 从 JSONL 计算 | 包含/不包含 |
| --- | --- | --- |
| App 进程到探针请求点 | `requestedNs - processStartNs` | 专用 Activity 激活的一段，不是生产首屏。 |
| 本阶段准备 | `prepareEndNs - prepareStartNs` | 清单加载和 fixture SHA 门之后，包含身份文件哈希、prepare 的解包或 run 的 wrapper/计划准备，以及首次 diskUsage；实际 guest 准备进程在其后启动。 |
| spawn 调用 | `spawnAfterNs - spawnBeforeNs` | `ProcessBuilder.start()` 返回所用时间。 |
| token / 端口等待 | `tokenNs - spawnBeforeNs` / `portReadyNs - spawnBeforeNs` | 同一 run 内；startup 的 CSV `wallNs` 用端口项。缺失保持 null，不伪造零耗时。 |
| 停止 | `stopEndNs - stopStartNs` | 包含身份扫描、等待与信号请求。 |
| pnpm / apt / Git 主命令 | `workload.main.wallNs` | fixture 从 spawn 到主命令退出的持续时间；检查/卸载/重启不并入主值，仍全部作为通过条件。 |

每个配对块按轮次旋转三方式顺序，减少固定顺序偏差。只对 PASS 样本算中位数，相同 round 与 PRoot 配对计算差值。至少 10 对才输出固定随机种子的 2,000 次重采样中位数区间；这是描述性区间，不能消除网络、缓存和连续轮次的相关性，不报告“稳定 p95”。FAILED/SKIPPED 数、退出和已观察耗时完整保留。

“冷”只指 App/guest 进程重启与每 unit 的新 APK 副本；不 drop caches、不重启设备，字段明确 `cold.osPageCache="retained"`。250 ms 所有权扫描、50 ms 就绪轮询、文件哈希和目录遍历本身有开销，debug 结果不能直接换算成正常 App 的生产性能。`fixtureParentCpuMicros` 只含 fixture 父 Node，不含子进程和 tracer CPU，不能当总 CPU 优劣。

`resources.before/after` 用 App `lstat` 统计**整个 unit**，按 device/inode 去重且不跟随 symlink；逻辑大小只计普通文件，分配大小按 `st_blocks×512`，包括目录等分配。`storeBefore/storeAfter` 是 unit 内 `tawcroot/` 子树，**不能与 unit 数相加**。前后值还包含探针元数据开销。pnpm 另记录 guest store 内容字节变化，两种口径不互相替代。

## 结果与停止后的处理

`results.jsonl` 是固定 schema 1 主表，字段见 `result-schema.json`；每条带 runId/round/session、请求与实际执行器、阶段/状态、退出、身份、时钟、停止证据和场景检查。`results.csv` 只是有限列投影，不含所有分段；`summary.json` 只统计符合通过条件的样本。每个 `<uuid>/` 子目录保存收到的 `prepare.json`、`run.json`、`cleanup.json` 原始结构，先落盘再验证，便于检查被 host 拒绝的矛盾结果。失败结构可能没有成功记录的所有身份/时钟字段，不能计作 PASS。

退出 0 表示所有计划行 PASS；退出 1 表示有失败或跳过；前置输入/工具错误通常为 2，并在 stderr 标 `FAILED`。宿主 deadline 后写本 unit 的 cancel，额外等待至多 15 秒确认 App 收尾；晚到的 PASS 仍标失败。不确认停止就保留 unit、中止后续尝试；清理成功不翻转原失败，清理再次失败独立保存在 `cleanup`/`cleanupError`。

停止复用既有 `RuntimeProcessOwnership` 的 UID、root 标记/cwd、PID starttime 和已捕获后代识别。身份保存为本 unit 的 `owned-pids.json`，Activity 重建后仍可复核，已开始的 run 不自动重试。TERM/QUIT/KILL 数是已发出的请求，不证明所有 handler 已接收。任何观测不完整都不能宣称干净；残留、强杀、输出未收尾使该行失败。具有不可读同 UID 进程的环境可能被保守拒绝，零已知残留不等于完整观测。

默认停止确认、结果取回后删除整个 unit（rootfs、store、dev-view 一起），不逐个搬运或导出 guest 硬链接视图。`--keep-sessions` 只在确认停止后保留 unit 供 owner 本地检查，不把它转为生产系统或换 PRoot 继续使用。失败且停止未确认时，先查记录，由 owner 在专用模拟器处置，不清用户 App 数据。

对已保留、身份完整的 unit，可以仅请求 Activity 的清理动作（将下面的 session 替换成结果中的 32 位值）：

```sh
adb -s emulator-5554 shell am start -W \
  -n io.github.ccch1mneyyy.dsh_app/.runtime.probe.TawcrootPrototypeActivity \
  --es session SESSION_FROM_RESULT --es action cleanup
adb -s emulator-5554 shell run-as io.github.ccch1mneyyy.dsh_app \
  cat files/tawcroot-prototype-cleanup/SESSION_FROM_RESULT.json
```

清理先核 marker 与进程身份，不能绕过未知停止状态。I/O 失败可能只删除部分 unit，此时报告失败，不承诺自动恢复。结束实验后由 owner 处理一次性模拟器，无需把 unit 变成任何用户环境。

## 当前可验证的证据边界

```sh
python3 -I runtime/tests/test_tawcroot_measure.py -v
node --check runtime/prototypes/tawcroot/fixtures/workloads.mjs
```

Python 小测实际消费 5040 的 `build()`/`source.json`，编译器和 adb 使用受控替身；核 APK/OUT 身份合同、原失败退出保留、deadline、停止门、固定 Git 参考、轮次统计和本地输入。现有宿主 Node 只运行小型本地子进程、分片 marker、Wasm 加法及 fixture 路径/状态检查；restart 的事件顺序用受控 child/socket 对象验证。这些不是 NDK 构建、Activity 编译、真实 DSH 重启或 Android App 域证据。最终兼容和性能结论必须由 owner 在上述一次性环境产生的原始结果支持。
