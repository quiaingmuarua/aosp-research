# 个人 Git 工作流

## 产品仓库

本仓库是产品与工具的主要编辑位置。修改、测试并 commit 后，运行 `lab setup aosp13 --revision <commit>`。该操作只同步研究项目，原版 AOSP 不会跟着改变。

源码树中的产品检出也是真实 Git 仓库。如果在那里修改，先提交并通过普通 Git fetch/cherry-pick 保存回个人仓库，再更新产品版本。lab 不会覆盖未提交文件或只存在于该检出中的提交。

如果是在实验树内提交产品代码，先将其保存为个人仓库中的明确分支，再整合到 main。这样即使 cherry-pick 生成了新提交号，原提交也不会变成仅靠 FETCH_HEAD 临时保留的对象。

```bash
# 在个人仓库执行，使用本次功能独有的导入分支名。
git fetch /path/to/aosp13-research/device/kyler/research HEAD:refs/heads/import/product-my-feature
git merge --ff-only import/product-my-feature
# 如果 main 已分叉，可改为 cherry-pick 所需提交，并保留上述 import 分支。
./lab setup aosp13 --revision main
```

## 核心实验

以下命令在**实验目录的 frameworks/base 子仓库**执行。基线来自 targets/aosp13.json。

```bash
git status --short
git switch -c research/a13/prototype 87725c3faa3f5d4e6ed838ad684d2bd49b5d9721
# 修改 ActivityManagerShellCommand，检查差异后提交。
git diff
git add services/core/java/com/android/server/am/ActivityManagerShellCommand.java
git commit -m 'Add a read-only research status shell command'
```

回到个人仓库运行完整构建、设备验证，然后 `lab snapshot experiment-1`。快照会将核心提交推送到 state/git/frameworks-base.git 的 snapshots/experiment-1 分支；在实验分支上也同步保存 research/a13/prototype。

返回基线前，停止 lab 模拟器，确认工作目录干净且已保存提交。仍在上述核心子仓库执行：

```bash
git status --short
git switch --detach 87725c3faa3f5d4e6ed838ad684d2bd49b5d9721
```

再运行完整构建、启动和 `lab verify --expect baseline`。恢复实验时使用 `git switch research/a13/prototype`，重新构建并验证。功能分支始终保留，不用 reset --hard 或删除分支来返回基线。

注意：切换 product 不会撤销 framework 提交；源码切换后，必须重建镜像才能在设备上观察对应行为。

## 保存与恢复

每个快照有 snapshot.json 与 manifest.xml。产品和 framework 的 Git 历史保存在 state/git，快照分支保留对应提交。可以通过 git clone --branch snapshots/<name> 从这些裸仓库恢复研究代码。

manifest 锁定其余上游项目的提交，并将两个私有项目指向实际 Git 保存位置。异机使用时更新这些本地远端地址；上游对象仍需从可用的上游或参考缓存取得。

## 部分克隆的边界

本机原 AOSP 是 blob:none 部分克隆。备份保存提交历史、当前版本已有文件和所有私有功能提交；上游历史中从未下载过的文件内容仍由官方远端提供。它不依赖原 aosp13 目录，但不等于全部 Android 历史文件的离线镜像。恢复核心仓库可以使用：

```bash
git clone --filter=blob:none --branch research/a13/prototype file:///path/to/state/git/frameworks-base.git restored-frameworks-base
```

在断开原开发检出后恢复私有提交的测试包含在维护脚本测试中；实际原型的恢复证据另见验证报告。
