# 实验 001：区分 product 扩展与核心改动

问题：希望保留上游版本，同时可以独立提交和恢复核心实验。

产品层只安装 research-info，使用公开的系统属性读取接口。核心层只在 ActivityManagerShellCommand 增加 research-status。选择这两个只读入口，是为了验证 Git 维护方法，同时保持系统行为容易对照。

实验提交：28887f3d5d74e8cfaf4ee5e0015470b09fa941d2。
基线提交：87725c3faa3f5d4e6ed838ad684d2bd49b5d9721。
保存分支：research/a13/prototype；备份：state/git/frameworks-base.git。

日常切换和恢复命令见 docs/git-workflow.md；四轮完整构建与冷启动结果见 docs/verification.md。移植到新 Android 版本时，先从该版本基线建立新分支，再 cherry-pick 此功能提交，重点检查命令分发入口、帮助位置和可调试构建判断。不要合并 Android 13 的整条上游历史。
