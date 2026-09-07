# AOSP 13 原型实测记录

已完成真实完整产品构建，以及“基线 → 核心实验 → 恢复基线 → 重新启用”的四轮冷启动。每轮使用新的 userdata，AOSP 13 自带模拟器 31.3.9、KVM、SwiftShader，固定 8 个构建并行任务。

## 提交组合

- 上游：android-13.0.0_r43；原 manifest 提交 559c9ae7f7b4d4596127c3472d7772eefef05037。
- 产品：research_x86_64-userdebug，继承 sdk_phone_x86_64；工具安装于 /product/bin/research-info。
- 四轮对照均使用产品提交 dd621c5647264975008f6504caf8743e5147731e。
- framework 基线：87725c3faa3f5d4e6ed838ad684d2bd49b5d9721。
- framework 实验：28887f3d5d74e8cfaf4ee5e0015470b09fa941d2，research/a13/prototype 分支。
- 核心改动只有 ActivityManagerShellCommand.java 的 11 行新增，命令和帮助均受 Build.IS_DEBUGGABLE 控制。没有另行构建 user 镜像；不可调试路径经过源码检查。
- 发布标签为 prototype-v0.1。发布整理后的产品提交与最终运行记录以 state/snapshots/prototype-v0.1 中的冻结记录为准。

## 四轮运行

时间为 Asia/Shanghai。所有行均在完整构建后启动，并通过全部 lab verify 检查。

| 阶段 | 验证时间 | framework 提交 | 核心命令结果 | 工具及系统检查 |
|---|---|---|---|---|
| 产品基线 | 09-08 00:05:07 | `87725c3faa3f` | Unknown command | 通过 |
| 启用核心实验 | 09-08 00:22:53 | `28887f3d5d74` | enabled / sdk=33 | 通过 |
| 恢复核心基线 | 09-08 00:29:46 | `87725c3faa3f` | Unknown command | 通过 |
| 重新启用实验 | 09-08 00:42:34 | `28887f3d5d74` | enabled / sdk=33 | 通过 |

research-info 的文本与 JSON 一致；SDK=33、Android=13、ABI=x86_64、产品 flavor 正确。fingerprint 与系统 getprop 读取值完全相同；错误参数返回 2。AOSP 的 activity help 正常显示帮助时返回 255，脚本同时校验返回值和帮助标题，避免误判。

四轮 research-info 二进制 SHA-256 均为 b02b2f69f9d3a3b28e7bb0599ef448fe02526b3126e3d0570ca58307d94547e4。返回基线时，设备中的 services.jar 哈希也恢复为初始值；重新启用时恢复为实验值。四轮崩溃缓冲区均为空。

首次完整构建约 67 分钟；工具修正后的完整增量构建为 19 秒，首次核心实验完整增量构建为 1 分 21 秒。详细日志和各次起止时间保存在 state/builds 中。

## Git 恢复和原环境

- 独立实验树包含 1135 个上游项目及 1 个产品项目。初始所有上游 HEAD 与原记录一致，工作树无未提交差异；对象库没有指向原源码的 alternates。
- 从实际本地裸仓库重新克隆了产品和 frameworks/base，禁用了 Git 的 HTTP、HTTPS、SSH 传输。在恢复检出中，核心基线和私有实验都可切换，私有文件内容完整。
- 快照清单中 1136 个提交全部可在本地取得：私有提交从裸 Git 备份检查，上游提交从已解除引用的实验源码检查。没有声称重新联网下载过整棵 AOSP。
- 原 AOSP 的 1135 个子仓库状态、641255 个输出文件的元数据和 9 个关键镜像哈希前后核对一致。Contacts 中原有文件删除和 libcore 中原有未跟踪文件均保留。
- 维护脚本 15 项测试通过，包括路径身份、脏文件、未完成 Git 操作、中断恢复、私有提交保存、部分克隆备份、构建失效记录、JSON 转义和长 fingerprint。

## 证据位置

state 为 .lab.local.json 中配置的状态目录。

- runtime/<阶段名>/verification.json、screen.png、logcat.txt、crashes.txt、binary-hashes.json
- snapshots/baseline-1 和 snapshots/experiment-1：Git 组合、锁定清单及完整镜像文件、SHA-256
- snapshots/restored-baseline-1、snapshots/restored-experiment-1：恢复演练的提交和验证记录
- snapshots/prototype-v0.1：发布冻结版本的镜像、提交和运行记录
- reproduction/core-source-1：实际独立克隆及恢复证据
- reference-before.json、reference-after.json、reference-comparison.json：原环境核对
- records/prototype-v0.1.json：本仓库保留的实测摘要，路径使用占位符

## 本机兼容处理

原源码为 blob:none 部分克隆。实验 Repo 复制已有对象和 promisor 标记后解除引用，避免 Git 2.34 的 preciousObjects/repack 冲突及历史缺失 blob 问题。备份端使用普通 git fetch --no-filter 回存，绕过 Git 2.34 接收已存在 promisor 提交时的挂起；新私有 blob 会完整保存，细节与来源见 git-workflow.md。

当前源码和私有实验可独立恢复；从未下载的上游历史文件仍可能需要官方远端。这不是全 Android 历史的全离线镜像。AOSP 14、15 尚未下载或验证。
