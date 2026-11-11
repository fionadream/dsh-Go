# 5040：tawcroot 构建原型（不合入）

这是一条独立的开发构建入口，供隔离、一次性环境的后续测量使用。现有 `runtime/build-runtime.sh` 和普通 APK 构建不会调用它。它不改变应用的默认启动方式、targetSdk、minSdk 或生产 rootfs。

## 固定来源和已有工具链

| 项目 | 固定值 |
|---|---|
| 上游仓库 | `wmww/tawc` |
| commit | `a49a4075228f4801dadc1eee584aed33fe8ea4ce` |
| 源码归档 | `https://github.com/wmww/tawc/archive/a49a4075228f4801dadc1eee584aed33fe8ea4ce.zip` |
| 归档 SHA256 | `74487243bde86c303e786e79cd4a13e98c3279cae4ab7725dcf5fa51c0b530c1` |
| NDK | 已有 `28.2.13676358`，检查 `source.properties` 的 `Pkg.Revision` |
| host | Linux x86_64，Python 3.11 或以上 |
| 编译 API | 29，与固定上游生产配方一致；这不是更改本应用 minSdk |
| 产物 ABI | 每次完整构建 `arm64-v8a` 和 `x86_64` |

本项目 Gradle 使用 `flutter.ndkVersion`。现有 Flutter 3.47.7 对应的固定 Flutter 提交 `abaf9c523780a608bd46686fd5e53740a07077f8` 中，[FlutterExtension.kt](https://github.com/flutter/flutter/blob/abaf9c523780a608bd46686fd5e53740a07077f8/packages/flutter_tools/gradle/src/main/kotlin/FlutterExtension.kt) 指定 `28.2.13676358`。本入口不会寻找最高 NDK 目录、安装工具链或改用 tawc 自己 App 的 NDK 版本。

`source.json` 固定归档身份、NDK/API 和 42 个生产 C 源文件。入口依据固定上游 [tawcroot/build.sh](https://github.com/wmww/tawc/blob/a49a4075228f4801dadc1eee584aed33fe8ea4ce/tawcroot/build.sh) 的生产配方逐文件编译，再链接、strip；每个 ABI 另加它的两个汇编文件。C/汇编和头文件原文不修改。相对源码路径及 `-Iinclude` 替代上游的绝对路径；不引入 testhost、cleat、STC 或整个上游 App，也不执行上游构建脚本的依赖下载/打包逻辑。

## 离线首次构建与缓存

在仓库根目录，使用已经取得且符合上述 SHA 的源码 ZIP：

```sh
python3 -I runtime/prototypes/tawcroot/build.py \
  --ndk "$ANDROID_HOME/ndk/28.2.13676358" \
  --archive /absolute/path/tawc-a49a4075228f4801dadc1eee584aed33fe8ea4ce.zip \
  --offline
```

也可通过 `ANDROID_NDK_HOME` 提供已有 NDK 目录，并省略 `--ndk`。原件校验通过后存入 `runtime/.cache/tawcroot/<完整 commit>.zip`；之后省略 `--archive` 即可离线复用。未提供原件、没有缓存且未指定 `--offline` 时，入口只尝试表中的固定官方 URL。传输失败或摘要不符即失败，不更新 pin；错误缓存不会被静默重下载替换，可用 `--archive` 显式导入正确原件。

构建在独立临时目录内完成，两 ABI 编译、strip、ELF 审计和清单全部成功后才替换 OUT。取料、编译或审计失败保留上次完整 OUT；临时文件清理。该开发入口按同一目录单进程使用。

## 输出合同

OUT 固定为 `runtime/.work/tawcroot-prototype/out`，不会写入普通 `runtime/out`：

| 相对 OUT 的路径 | 内容 |
|---|---|
| `jniLibs/arm64-v8a/libtawcroot.so` | arm64 生产 launcher |
| `jniLibs/x86_64/libtawcroot.so` | x86_64 生产 launcher |
| `manifest.json` | 两 ABI 的身份、工具链、配方和审计记录 |
| `assets/tawcroot-prototype/manifest.json` | 与根清单逐字一致的 APK 资产副本 |
| `assets/tawcroot-prototype/LICENSE.MIT` | 固定归档中的完整上游 MIT 许可原文 |
| `assets/tawcroot-prototype/THIRD_PARTY_NOTICES.draft.md` | 本原型署名与采用范围草稿 |

清单 `schema=1`。`source` 含 `repo/commit/archiveSha256`；另记录 `ndkVersion`、实际执行的 `clang --version` 和 `ld.lld --version` 文本。`flags[abi]` 包含 compiler 名称、compile/link 参数数组和相对源码列表。`artifacts` 每项有 `abi/path/sha256/bytes` 以及 `elf.type/machine/interpreter/loadAlignment/needed/entry`。摘要和大小在 strip 后读取，清单不以待执行的配方冒充已生成产物。

产物须为对应 machine 的 64-bit ET_EXEC、无 PT_INTERP、无 DT_NEEDED，所有 PT_LOAD 均满足 16 KiB 段对齐及地址/偏移同余；入口须处于上游固定基址附近。读取 ELF 复用现有 `runtime/lib/rtbuild.py`。这些文件使用 `.so` 名称是为了 Android native library 提取约定，**不是供 `System.loadLibrary` 加载的共享库**。

## 显式打入隔离 debug APK

先由现有流程准备需要的普通 runtime 资产，再完成上面的原型构建。仅本次命令显式开启：

```sh
DSH_TAWCROOT_PROTOTYPE=1 bash tooling/build-apk.sh --debug -f x64
# arm64 一次性设备使用同一 OUT 和 -f arm64。
```

5040 仅将该 OUT 的 JNI/assets 接入 `debug` sourceSet；未设置开关的 debug、profile 和所有 release 均不接入这些原型文件。即使 release 构建带着该变量，也没有 release sourceSet 接线。JNI 提取沿用现有 `useLegacyPackaging=true`；开启时保留 `libtawcroot.so` 的符号处理结果，避免 AGP 再 strip 改变清单摘要。实际打包后的字节一致性仍由 owner 验证。

5041 的纯参数映射和 5042 的调试 Activity/测量脚本另行交付。场景 fixture 由测量入口显式送入本次会话，不在本补丁中加入普通 `src/debug/assets`。仅构建或加入该 launcher 不表示 guest、Node、PTY、停止流程或高 targetSdk 已兼容。

## 当前验证与待实测项

```sh
python3 -I runtime/tests/test_tawcroot_build.py -v
```

轻测使用真实临时文件/ZIP 与合成 ELF 元数据；编译器、strip、版本查询和网络均由 Python mock 接管。覆盖离线缓存、错摘要/传输失败、NDK 版本选择、源码提取范围、两 ABI 清单/许可、ELF 拒绝项、失败时保留旧 OUT 和发布失败恢复。原型是新增入口，无旧实现的回归“先失败”结论。

本补丁作者没有执行 NDK、Flutter、上游程序或设备测试。owner 后续需用现有 NDK 实际编译两 ABI，保留清单及产物；检查 debug 开关关闭/开启和 release APK 的条目及提取后的 SHA；再按 5042 执行真实 App 域测量。Python 通过不代替这些结论，本原型不单独发代跑请求。
