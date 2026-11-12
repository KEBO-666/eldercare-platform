# GitHub公开发布记录

发布日期：2026年11月12日。参赛人唐克波，本科B组，参赛编号2132417。

公开仓库：<https://github.com/KEBO-666/eldercare-platform>，默认分支main，评委无需登录即可查看与克隆。

## 公开范围与版本

公开版本是原本地仓库的独立过滤副本，保留原9次提交的顺序及日期，排除全部历史中的数据库、WAL/SHM、运行数据、缓存与私有配置。原始本地历史未改写；公开仓库的提交哈希发生变化，详见公开仓库`submission/commit-map.json`。公开版本说明位于`docs/chuanzhi/公开版本说明.md`。

实际发布材料准备按当时时间追加提交，未虚构早期迭代。首次公开验收HEAD为`a2c11b5dc636d7fa5a0f99c7cd14b4986b392b07`；后续报告整理只更新文档与验证材料。

## 访问与完整性实测

北京时间18:14完成匿名GitHub API、匿名Git远程引用及PDF/视频完整下载核验：

- 仓库`visibility=public`，默认分支`main`。
- 公开12次提交中的252个唯一历史路径均完成检查；data目录只有两个新补充的空占位文件。
- `submission/SHA256SUMS.json`列出的20份材料与Git对象字节哈希全部一致。
- 18页PDF匿名下载HTTP200，660510字节，SHA-256为`90aec59e9e9a64ce042dc206406f3bb55226021e198d470e6887296629b69b17`。
- 7分09秒视频匿名下载HTTP200，27317789字节，SHA-256为`a6607f5bf249ffd3df36a190bd039fe050f22c8c788925baa41860c4146464c5`。

报告：`reports/github-publication.json`；公开仓库中保存副本于`submission/verification/github-publication.json`。

## 远程取得后的运行验证

北京时间18:13开始，匿名克隆至六类角色登录与13次页面访问全部HTTP200，用时90.484秒（含自有服务停止合计90.781秒）。其中克隆4.094秒、建全新venv9.250秒、依赖网络安装60.266秒、pip check 1.562秒、依赖冻结记录0.656秒、初始化与启动及HTTP检查9.547秒。初始venv、数据库和.env均不存在，pip缓存为0字节；Python3.12.14预安装，网络可用，使用当时配置的包索引。未计入Python安装或全量pytest。验证后自有服务进程树已停止，端口释放。

独立运行结果见`reports/github-remote-reproduction.json`，公开副本保存于`submission/verification/github-remote-reproduction.json`。公开应用94个Git blob与原来源提交`5524ab578eb154fe782743cb0dd7cdab0b4a2c5d`一致，避免Windows换行转换对比较造成影响。具体条件以报告为准。

## 材料时间与证据边界

旧PDF、视频、历史准备文档及第一次ZIP中的“尚无远程仓库”是发布前状态。本记录补充公开地址和实际访问结果，不把后续发布行为回填为此前已经完成。

已完成410项测试、89.14%覆盖率及28项Edge检查。早期Git只有两次粗粒度提交，已有AI记录尚不能证明三条独立完整人类Prompt链，此限制继续保留。仓库公开不等于赛事平台上传，尚无赛事平台提交回执。

GitHub插件可读取新仓库，但创建仓库接口返回403；实际创建通过参赛人已登录的Edge界面完成。参赛人亲自完成Git Credential Manager标准授权后，独立过滤副本由Git推送，未读取或输出浏览器Cookie、密码或令牌。
