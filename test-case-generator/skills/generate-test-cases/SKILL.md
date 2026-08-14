---
name: generate-test-cases
description: 自主学习型测试文档生成器。从需求文档（Markdown）生成测试用例 XMind/XLSX 文件，支持持久化记忆和持续学习。当用户提到"生成测试用例"、"根据需求生成测试"时触发。
compatibility: Requires Python 3
---

# 根据需求生成测试用例

从需求文档自动生成专业测试用例，具备深度解析、质量自检、自主学习能力。

## 核心能力

- **深度解析**：4 种测试设计方法（EP/BVA/ST/EG）系统性覆盖需求
- **质量内建**：覆盖率验证 + 优先级分布检查 + 自检门禁，生成前拦截缺陷
- **需求追溯**：自动建立需求↔用例双向追溯矩阵，量化覆盖率
- **自主学习**：持久化记忆 + 历史趋势分析 + 歧义决策复用，越用越准
- **交互式确认**：快速模式，关键节点人工把关

## 质量标准

生成的测试用例必须满足以下 10 维度（Phase 2.8 质量预审时逐项检查）：

| 维度 | 标准 | 检查方式 |
|------|------|----------|
| **需求覆盖** | 每条需求至少关联 1 条用例，覆盖率 ≥ 95% | 追溯矩阵计算 |
| **方法覆盖** | 每条需求至少使用 1 种设计方法，复杂需求 ≥ 2 种 | 方法分布统计 |
| **优先级分布** | P0: 10-15%, P1: 30-40%, P2: 30-40%, P3: 10-20% | 分布比例检查 |
| **步骤可执行** | 每条用例的步骤明确、可操作，预期结果可验证 | AI 自检 |
| **无需求外编造** | 所有用例来源于需求文档，不凭空编造场景 | 追溯关系验证 |
| **术语一致** | 用例中使用的术语与需求文档、`terminology.json` 一致 | 术语对照 |
| **无冗余重复** | 不同设计方法产生的用例无语义重复 | 去重扫描 |
| **派生字段校验** | 所有依赖其他输入项自动计算的只读/计算字段，均生成推导算法与编辑性控制校验用例 | 派生字段清单扫描 |
| **状态机覆盖** | 每个业务实体的完整生命周期状态均覆盖，含按钮活性、字段可编辑性矩阵 | 状态转移图校验 |
| **业务闭环** | 用例覆盖输入文档中提及的每一个功能模块和业务实体，禁止只生成部分子模块 | 模块清单对照 |

## 交互模式

详见 [INTERACTION-PATTERNS.md](references/INTERACTION-PATTERNS.md)

采用**快速模式**：回车继续，仅在发现问题或异常时询问用户。

### 交互检查点

| 检查点 | 阶段 | 行为 |
|--------|------|------|
| 解析确认 | Phase 2.5 | 摘要 + 有问题时询问 |
| 歧义处理 | Phase 2.6 | 仅关键歧义 |
| 生成预览 | Phase 2.8 | 统计数据 |

## 测试设计方法

> 详细定义与示例见 [TEST-DESIGN-METHODS.md](references/TEST-DESIGN-METHODS.md)

**4 种方法按序叠加使用**：

```
EP 等价类划分 → BVA 边界值分析 → ST 场景法 → EG 错误推测
```

| 方法 | 核心动作 | 触发条件 |
|------|---------|---------|
| **EP 等价类** | 划分有效/无效等价类，无效类单独覆盖 | 有输入范围、格式、枚举约束 |
| **BVA 边界值** | 测试上点、离点、内点 | 有数值/长度/时间边界 |
| **ST 场景法** | 基本流→备选流→异常流各生成用例 | 涉及多步骤业务流程 |
| **EG 错误推测** | 补充特殊字符、极端值、并发场景 | 高风险模块、历史缺陷多 |

## 需求追溯与覆盖率

> 详细规范见 [TRACEABILITY.md](references/TRACEABILITY.md)

- **需求ID**：自动识别 `REQ-xxx`、`F1.2`、`US_042`、`PROJ-123` 等模式；无ID时生成 `MOD_{缩写}_{序号}`
- **双向追溯**：需求→用例 + 用例→需求，Phase 2.8 输出覆盖率统计
- **覆盖率目标**：≥ 95%，未覆盖需求在预览中高亮警告

## 优先级与回归分类

> 详细规则见 [TEST-PRIORITY.md](references/TEST-PRIORITY.md)

| 级别 | 来源 | 回归集 |
|------|------|--------|
| **P0** 核心 | 基本流用例 | 冒烟测试（每次构建） |
| **P1** 主要 | 备选流 + 边界值 | 核心回归（每日/提测） |
| **P2** 次要 | 异常流 + 错误推测 | 全量回归（发版前） |
| **P3** 边缘 | 边缘错误推测 | 全量回归（发版前） |

## .memory 记忆系统

> 完整 Schema 定义见 [MEMORY-SCHEMA.md](references/MEMORY-SCHEMA.md)

Skill 在项目中创建 `.memory/` 文件夹，存储跨会话学习数据：

```
.memory/
├── project-context.json      # 项目上下文（路径、名称）
├── terminology.json          # 领域术语库（自动学习 + 手动补充）
├── generation-history.json   # 生成历史（质量趋势分析）
├── user-preferences.json     # 用户偏好（交互模式、默认标签）
└── ambiguity-decisions.json  # 歧义决策记录（避免重复询问）
```

## Workflow

### Phase 0: 项目初始化（首次运行）
1. 检测项目结构（扫描 `requirements/`、`test-docs/` 等目录）
2. 创建 `.memory/` 文件夹（`memory_manager.py --action init`）
3. 从需求文档提取领域术语，存入 `terminology.json`
4. 保存用户偏好到 `user-preferences.json`

### Phase 1.0: 文档预处理
1. 扫描 `requirements/` 目录下的 Word / PDF 源文件
2. 如发现任何 Word / PDF 源文件，自动调用 `scripts/requirements_preprocessor.py` 进行预处理
3. 预处理器将源文件转换为同名 `.md` 文件
4. 如果对应的 `.md` 已存在，则直接跳过

### Phase 1: 读取需求
1. 读取 `requirements/` 目录下所有 `.md` 文件，按文件名排序，文件间以 `---` 分隔拼接
2. 读取 `requirements/` 目录下所有图片文件，与需求文本一起作为多模态输入

### Phase 2: 解析需求
1. 加载记忆并应用（terminology / ambiguity-decisions / generation-history / user-preferences）
2. 提取需求ID
3. 结构识别：功能模块、验收条件、业务规则
4. 系统性测试设计（EP→BVA→ST→EG 顺序叠加）
5. 深层业务逻辑挖掘（派生字段/状态机/数据权限）
6. 去重扫描
7. 歧义检测
8. 覆盖率预计算

> 详细解析规则见 [PARSING-RULES.md](references/PARSING-RULES.md)

### Phase 2.5: 解析确认（检查点1）
使用 AskUserQuestion 确认解析结果，仅在发现警告或无 default_tag 时询问

### Phase 2.6: 歧义处理
对检测到的歧义需求逐一询问，仅询问影响 P0/P1 的关键歧义

### Phase 2.8: 质量预审 & 生成预览（检查点2）
13 维度质量自检，通过则继续，不通过则修正后重新自检

### Phase 3: 生成文档
1. 分配优先级
2. 关联需求ID
3. 用例写作规范（详见 SKILL.md 正文）
4. 将用例组织为 JSON 数组，写入 `tmp/cases_<时间戳>.json`
5. 调用 `scripts/generate_xmind.py` 生成 XMind 文件
6. 调用 `scripts/generate_xlsx.py` 生成 XLSX 文件

### Phase 4: 更新记忆（自学习闭环）
1. 写入 `generation-history.json`
2. 歧义决策写入 `ambiguity-decisions.json`
3. 新术语更新 `terminology.json`
4. 用户偏好保存到 `user-preferences.json`

> 完整学习规则见 [LEARNING-RULES.md](references/LEARNING-RULES.md)

## 输出格式

### 测试用例 XMind

固定节点结构（符合 XMind 导入规范）：

```
根节点（项目名称）
  └── 模块（最多8层）
        └── tc-p0: 用例标题  /  tc: 用例标题（无优先级时）
              ├── pc: 前置条件（非必填）
              ├── 步骤1
              │     └── 预期结果1
              ├── 步骤2
              │     └── 预期结果2
              └── tag:标签1,标签2（非必填）
```

### JSON 字段

| 字段 | 必填 | 说明 |
|------|------|------|
| `模块` | ✅ | 数组，最多8层，末级按功能区域分组 |
| `用例标题` | ✅ | 格式：`动作 + 对象 + 条件/场景` |
| `优先级` | ✅ | P0/P1/P2/P3 |
| `需求ID` | ✅ | 关联的需求标识 |
| `设计方法` | ✅ | `EP`/`BVA`/`ST`/`EG` 之一或多个 |
| `前置条件` | ❌ | 生成 `pc:` 子节点 |
| `步骤` | ✅ | `[{"操作": "1. ...", "预期": "1. ..."}]` |
| `标签` | ✅ | 测试端标识 |

## 脚本调用

### 生成 XMind
```bash
python scripts/generate_xmind.py -f tmp/cases_<timestamp>.json -o test-docs/testcases_<timestamp>.xmind
```

### 生成 XLSX
```bash
python scripts/generate_xlsx.py -f tmp/cases_<timestamp>.json -o test-docs/testcases_<timestamp>.xlsx
```

### 管理记忆
```bash
python scripts/memory_manager.py --action init --project .
python scripts/memory_manager.py --action add-record --project . --data '{...}'
```

## 约束

### 质量底线
- 需求追溯：所有用例必须关联需求ID
- 方法标记：所有用例必须标记至少 1 种设计方法
- 优先级分布：P0: 10-15%, P1: 30-40%，P2 补足剩余，P3 ≤ 20%
- 覆盖率：≥ 95%
- 无编造：不凭空编造需求中不存在的场景
- 无冗余：不同设计方法产生的语义重复用例必须合并
- Scope 完整性：必须覆盖需求文档中提及的每一个功能模块

### 标签规则
- 需求有显式平台声明时直接读取，无需询问
- 无显式声明时在 Phase 2.5 询问一次，全程不再重复询问
- 用户输入原样写入，禁止翻译、展开或同义替换

### 交互规则
- 仅在发现问题/异常时询问用户
- 无异常时自动继续执行

### 其他
- 文件命名中的时间戳必须使用 `YYYYmmddHHMMSS`（14 位）
- 默认输出中文
- `.memory` 文件夹应加入 .gitignore
- 禁止复用 tmp/ 缓存：每次生成均从 Phase 1 重新开始

## 参考文档

- [INTERACTION-PATTERNS.md](references/INTERACTION-PATTERNS.md) - 交互模式与问题模板
- [TEST-DESIGN-METHODS.md](references/TEST-DESIGN-METHODS.md) - 核心4种测试设计方法详解
- [TRACEABILITY.md](references/TRACEABILITY.md) - 需求追溯矩阵规范
- [TEST-PRIORITY.md](references/TEST-PRIORITY.md) - 优先级与回归分类
- [PARSING-RULES.md](references/PARSING-RULES.md) - 需求解析规则
- [MEMORY-SCHEMA.md](references/MEMORY-SCHEMA.md) - 记忆文件结构
- [LEARNING-RULES.md](references/LEARNING-RULES.md) - 学习规则
- [BUSINESS-RULES.md](references/BUSINESS-RULES.md) - 业务测试规则
