<!-- 感谢提交 PR！请把下面每一项都填掉，空的会被要求补充。 -->

## 这个 PR 解决什么

<!-- 问题/需求是什么。若是缺陷，写清现象与复现方式；若是功能，写清使用场景。 -->

关联 Issue：#

## 改了什么

<!-- 文件/函数级别即可，不需要逐行解释。 -->

-
-

## 怎么验证的

<!-- 请贴真实命令与真实输出摘要。不要写"应该没问题"。 -->

```powershell
# 例如
& build\venv\Scripts\python.exe scripts\selfcheck.py
```

结果：

- [ ] `scripts/selfcheck.py` 全绿（50 项）
- [ ] `scripts/selfcheck_design.py` 全绿
- [ ] `scripts/check_no_secrets.py --history` 无发现
- [ ] 改过前端：`npm run build` 通过，布局/规格卡片体检 0 缺陷
- [ ] 改过接口：`docs/API-CONTRACT.md` 已同步
- [ ] 改过依赖：`docs/LICENSE-THIRD-PARTY.txt` 已同步
- [ ] 改过生成/修改链路：跑过端到端（`scripts/e2e_test.py`）

## 副作用与兼容性

<!-- 有没有影响既有行为？老项目/老配置还能不能用？有没有性能影响？ -->

## 自查

- [ ] 没有提交任何密钥、令牌、Cookie、`.env` 或本机路径
- [ ] 没有提交 `build/`、`webui/dist/`、`node_modules/` 等生成物
- [ ] 没有为了让测试变绿而放宽断言（若改了判据，已在下面说明原因）
- [ ] 改动聚焦，没有夹带无关的格式化/重命名

## 备注

<!-- 判据变更说明、设计取舍、后续计划等。 -->
