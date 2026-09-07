# 个人 AOSP 研究环境

一个个人 Git 仓库维护 product、独立工具和维护脚本。需要修改 framework 时，在实验源码的对应 Git 子仓库保留功能分支。

当前实现目标是 AOSP 13 `android-13.0.0_r43`。完整构建、启动和恢复结果记录在 `docs/verification.md`；在验证完成前，请勿将代码存在视为原型已通过验收。AOSP 14、15 尚未验证。

## 第一次使用

需要 Linux、Python 3、Git、已提交的本仓库，以及已有 AOSP 13 参考源码。

```bash
./lab configure --source /path/to/aosp13 --tree /path/to/aosp13-research --state /path/to/research-state
./lab setup aosp13 --revision main
./lab status
./lab build
./lab run --name baseline-1
./lab verify --expect baseline
./lab stop
./lab snapshot baseline-1
```

构建默认使用 8 个并行任务，输出在实验源码自己的 `out`。模块迭代可以使用 `./lab build research-info`，但模块构建不会替代完整产品构建记录。

`setup` 首次创建独立 Repo checkout，利用参考源码对象并 dissociate。原环境是部分克隆；为兼容本机 Git 2.34 与 Repo 2.59，实验目录的 Repo 在解除引用前复制已有对象和 promisor 标记。原 Repo 不修改，也不补下载整段历史的缺失 blob。复制完成后，实验对象库没有指向原目录的 alternates。此后只更新个人产品项目。`--revision` 可以指定本仓库提交、分支或标签，最终以提交号固定。

## 两类修改

- **产品和工具**：修改本仓库，测试并提交，再执行 `./lab setup aosp13 --revision <提交>` 更新实验树中的独立检出。
- **framework**：进入实验源码的 `frameworks/base`，按 `docs/git-workflow.md` 建分支、提交、保存和返回基线。不要修改参考源码目录。

`research-info` 安装到 `/product/bin/research-info`。无参数输出文本，`--json` 输出字段值为字符串的 JSON 对象，`--help` 显示帮助；错误参数返回 2。

核心实验增加 `adb shell cmd activity research-status`，仅在可调试构建提供。返回 framework 基线后，该命令应消失，product 中的 research-info 仍保留。

## 模拟器和快照

每次运行使用新的 `--name`，保留自己的 userdata、日志和截图。默认端口 5580；`lab` 只操作登记且身份匹配的进程与 adb serial。构建前先停止模拟器。

```bash
./lab verify --expect experiment
./lab snapshot experiment-1
./lab protect --compare
```

快照在 state 目录记录 Git 提交与锁定 manifest，并将产品和 framework 历史保存到本地裸仓库。`lab snapshot <name> --images` 还会复制与源码匹配的完整产品镜像并保存 SHA-256。它不是整块磁盘的备份，也不会包含未提交代码。需要异机恢复时，应一并搬运这些裸仓库并更新清单中的本地来源。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

详见 PLAN.md、docs/git-workflow.md、docs/add-android-version.md。
