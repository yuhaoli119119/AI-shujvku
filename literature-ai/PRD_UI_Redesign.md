# PRD: literature-ai 前端 UI 重设计

> **项目名称**: literature-ai-ui-redesign  
> **文档版本**: v1.0  
> **日期**: 2026-05-24  
> **语言**: 中文  
> **技术栈**: 纯 HTML / CSS / JS（无框架），后端 FastAPI  
> **项目路径**: `D:\Desktop\代码开发\AI检索数据库\literature-ai\`

---

## 1. 产品目标

**将 literature-ai 前端从当前风格不统一的页面集合，重设计为一套拥有 6 种主题风格（含明暗/护眼模式）、顶部统一导航、设计令牌驱动的专业级 UI 系统，使用户在学术文献管理全流程中获得一致、高效、美观的交互体验。**

---

## 2. 用户故事

| # | 用户故事 |
|---|---------|
| US1 | 作为一名科研人员，我想在文献库、DFT 数据库、AI 写作等不同功能页面之间通过顶部导航一键切换，以便快速跳转而不迷失方向 |
| US2 | 作为一名长时间阅读文献的用户，我想在明暗、护眼模式之间自由切换，以便在夜间或长时间使用时保护视力 |
| US3 | 作为一名偏好个性化的用户，我想在 Expressive / Gradient / Impeccable / Material / Neumorphism / Refined 六种主题风格中自由选择，以便界面风格符合我的审美偏好 |
| US4 | 作为一名需要连接 AI 的用户，我想在设置页按功能分组快速找到 API 配置和 IDE 连接提示词，以便高效完成配置 |
| US5 | 作为一名新用户，我想在设置页直接查看使用说明，以便无需外部文档即可上手 |

---

## 3. 需求池

### P0 — Must Have（必须实现）

| ID | 需求 | 验收标准 |
|----|------|---------|
| P0-1 | **6 种主题风格切换** | 用户可在任意页面通过顶部导航的主题选择器切换 Material / Expressive / Gradient / Impeccable / Neumorphism / Refined 六种风格，切换后所有页面元素即时响应 CSS 变量变更 |
| P0-2 | **明暗模式 + 护眼模式** | 每种主题支持 Light / Dark / Eye-care 三种模式；通过 `data-theme` + `data-mode` 属性联动；护眼模式背景色 ≤ 200nit，文字对比度 ≥ 4.5:1 |
| P0-3 | **顶部统一导航栏** | 所有 9 个页面共享同一顶部导航组件（Brand + 8 个功能导航项均匀排列 + 主题/模式切换器）；导航项间距均匀，当前页高亮；导航高度 56px，sticky 固定顶部 |
| P0-4 | **CSS 设计令牌系统** | 建立全局 `tokens.css`，通过 CSS 自定义属性统一管理颜色、字体、间距、圆角、阴影等令牌；6 种主题 × 3 种模式 = 18 套令牌变量 |
| P0-5 | **共享布局组件** | 提取 topnav / sidebar / card / button / input / badge 等公共组件到共享 CSS，所有页面引用；消除各页面内联重复样式 |

### P1 — Should Have（应该实现）

| ID | 需求 | 验收标准 |
|----|------|---------|
| P1-1 | **设置页分组导航优化** | 设置页内部按功能分为：API 配置 / IDE 连接 / 外观主题 / 使用说明 四个分组，每组有独立导航按钮，点击滚动至对应区块 |
| P1-2 | **主题预览页整合** | 将 theme-preview 页面功能整合进设置页的"外观主题"分组，用户可在设置页直接预览和切换主题，无需单独页面 |
| P1-3 | **IDE 连接提示词功能优化** | 在设置页"IDE 连接"分组中，提供一键复制提示词按钮；提示词内容从项目 prompts 目录自动读取；复制后显示成功反馈 |
| P1-4 | **响应式适配** | 页面在 1280px+ 正常展示；1024-1280px 导航项自动收缩为图标+文字；< 1024px 侧边栏折叠为可展开面板 |

### P2 — Nice to Have（锦上添花）

| ID | 需求 | 验收标准 |
|----|------|---------|
| P2-1 | **页面切换过渡动画** | 导航切换时页面 fade-in（200ms ease）；主题切换时所有颜色属性平滑过渡（300ms ease） |
| P2-2 | **主题偏好持久化** | 用户选择的主题和模式存储至 localStorage，下次访问自动恢复 |
| P2-3 | **高级自定义** | 允许用户自定义主色调（color picker）；自定义字体大小（3 档缩放） |
| P2-4 | **键盘导航增强** | Tab 键可遍历所有导航项和交互元素；Enter/Space 触发操作；焦点可见环样式明确 |

---

## 4. UI 设计规范概要

### 4.1 六种主题核心设计令牌汇总

| 令牌 | Material (默认) | Expressive | Gradient | Impeccable | Neumorphism | Refined |
|------|----------------|------------|----------|------------|-------------|---------|
| **Primary** | `#6442D6` | `#db2777` | `#990FFA` | `#CC8800` | `#006666` | `#3B82F6` |
| **Primary Hover** | `#7c5ce0` | `#be1d66` | `#7d0dd4` | `#A66E00` | `#004d4d` | `#2563EB` |
| **Secondary** | `#C8B3FD` | `#2563eb` | `#E60076` | `#C55221` | `#F1F2F5` | `#8B5CF6` |
| **Surface (Light)** | `#FFFFFF` | `#FFFFFF` | `#FFFFFF` | `#FFFFFF` | `#E7E5E4` | `#FFFFFF` |
| **Background (Light)** | `#F5F6FA` | `#F9FAFB` | `#F5F3FF` | `#FEF9F0` | `#E7E5E4` | `#F8FAFF` |
| **Text** | `#111827` | `#111827` | `#111827` | `#111827` | `#1E2938` | `#111827` |
| **Border** | `rgba(0,0,0,0.08)` | `rgba(0,0,0,0.06)` | `rgba(0,0,0,0.06)` | `rgba(0,0,0,0.08)` | `rgba(0,0,0,0.0)` | `rgba(0,0,0,0.05)` |
| **Font Body** | Inter | IBM Plex Mono | Montserrat | Chakra Petch | Space Mono | Playfair Display |
| **Font Display** | Roboto | IBM Plex Mono | Space Grotesk | Chakra Petch | Space Mono | Playfair Display |
| **Font Mono** | Fira Code | IBM Plex Mono | JetBrains Mono | JetBrains Mono | JetBrains Mono | JetBrains Mono |
| **Type Scale** | 12/14/16/20/24/32 | 14/16/18/24/32/40 | 12/14/16/18/24/30/36 | 12/14/16/20/24/32 | desktop-first | 12/14/16/20/24/32 |
| **Spacing** | 4/8/12/16/24/32 | 4/8/12/16/24/32 | 8pt baseline | 4/8/12/16/24/32 | compact | 4/8/12/16/24/32 |
| **Radius** | 8px / 12px | 8px / 8px | 8px / 12px | 8px / 8px | 8px / 12px | 4px / 4px |
| **Shadow Card** | `0 1px 3px rgba(0,0,0,0.08)` | `0 2px 8px rgba(219,39,119,0.08)` | `0 4px 16px rgba(153,15,250,0.10)` | `3px 3px 0 rgba(204,136,0,0.15)` | `6px 6px 12px / -6px -6px 12px` | `0 1px 2px rgba(0,0,0,0.04)` |
| **Shadow Elevated** | `0 4px 12px rgba(0,0,0,0.10)` | `0 8px 24px rgba(219,39,119,0.12)` | `0 8px 32px rgba(153,15,250,0.15)` | `5px 5px 0 rgba(197,82,33,0.15)` | `8px 8px 16px / -8px -8px 16px` | `0 2px 8px rgba(0,0,0,0.06)` |
| **Visual 风格** | modern, minimal, clean | modern, playful | modern, playful | modern, clean, high-contrast | minimal, clean, tactile | modern, minimal, elegant |

### 4.2 明暗 + 护眼模式规则

| 模式 | 背景色策略 | 文字色策略 | 对比度要求 |
|------|-----------|-----------|-----------|
| **Light** | 各主题默认 surface/bg | 默认 text | ≥ 4.5:1 (AA) |
| **Dark** | surface → `#161822`，bg → `#0f1117` | text → `#e8eaed`，secondary → `#9aa0a6` | ≥ 4.5:1 (AA) |
| **Eye-care** | surface → `#F5F0E8`（暖黄），bg → `#EDE6D6` | text → `#3D3A35`（暖棕），secondary → `#7A756D` | ≥ 4.5:1 (AA)，蓝光 ≤ 200nit |

### 4.3 全局通用令牌

```css
/* 不随主题变化的固定令牌 */
--success: #16A34A;
--warning: #D97706;
--danger: #DC2626;
--nav-height: 56px;
--sidebar-width: 320px;
--transition: 0.2s ease;
--z-nav: 100;
--z-modal: 1000;
--z-tooltip: 1100;
```

---

## 5. 页面清单 — 各页面改造范围

### 5.1 通用改造（所有页面）

| 改造项 | 说明 |
|--------|------|
| 引入共享 `tokens.css` + `components.css` + `topnav.css` | 每个页面 `<link>` 引入，删除内联重复样式 |
| 替换顶部导航为统一组件 | 当前 literature_library 用 `.quick-links`，settings 用 `.topnav`，需统一为同一结构 |
| CSS 变量替换硬编码色值 | 所有 `#xxxxxx` 颜色替换为 `var(--xxx)` |
| 添加 `data-theme` + `data-mode` 属性 | `<html>` 标签上设置，JS 切换 |

### 5.2 各页面具体改造

| 页面 | 目录 | 当前状态 | 改造重点 |
|------|------|---------|---------|
| **文献库主页** | `literature_library/` | 最复杂页面（97KB），侧边栏+工作区双栏，含搜索/筛选/文献列表/详情 | 重构布局为 `grid: 320px 1fr`；所有组件适配 CSS 变量；侧边栏卡片、论文列表、搜索栏等适配 6 主题 |
| **论文详情页** | `paper_detail/` | 骨架页（651B） | 按设计令牌构建完整详情页布局：标题区 + 元信息 + 标签 + Tab 面板 |
| **DFT 数据库** | `dft_database/` | 骨架页（322B） | 按设计令牌构建数据表格布局：筛选器 + 表格 + 分页 |
| **机理知识库** | `mechanism_knowledge/` | 骨架页（319B） | 按设计令牌构建知识图谱布局：搜索 + 卡片列表 |
| **写作卡片** | `writing_cards/` | 骨架页（435B） | 按设计令牌构建卡片瀑布流布局 |
| **AI 写作助手** | `ai_writer/` | 骨架页（641B） | 按设计令牌构建对话式布局：侧栏历史 + 主区对话 |
| **外部分析工作台** | `external_analysis_workbench/` | 骨架页（668B） | 按设计令牌构建工作台布局：文件上传 + 结果面板 |
| **设置页面** | `settings/` | 已有完整功能（40KB），暗色主题，含分组导航 | 重构为 4 分组导航（API配置/IDE连接/外观主题/使用说明）；外观主题组合并主题预览功能；适配 6 主题 + 3 模式 |
| **主题预览** | `theme-preview/` | 已有完整功能（25KB），6 主题 + 明暗切换 | 功能整合进设置页"外观主题"分组；此页面改为重定向至设置页或保留为独立预览展示 |

---

## 6. 文件结构规划

```
frontend/
├── shared/
│   ├── tokens.css          # 设计令牌：6主题 × 3模式 变量定义
│   ├── components.css      # 公共组件样式：button/card/input/badge/tab/modal
│   ├── topnav.css          # 顶部导航栏样式
│   └── topnav.js           # 顶部导航 + 主题/模式切换逻辑
├── pages/
│   ├── literature_library/
│   │   └── index.html
│   ├── paper_detail/
│   │   └── index.html
│   ├── dft_database/
│   │   └── index.html
│   ├── mechanism_knowledge/
│   │   └── index.html
│   ├── writing_cards/
│   │   └── index.html
│   ├── ai_writer/
│   │   └── index.html
│   ├── external_analysis_workbench/
│   │   └── index.html
│   ├── settings/
│   │   └── index.html
│   └── theme-preview/
│       └── index.html      # 可保留或重定向
```

---

## 7. 待确认问题

| # | 问题 | 影响 | 建议 |
|---|------|------|------|
| Q1 | 护眼模式的色温偏移是否需要跨所有主题统一，还是每个主题各自定义暖色调？ | Dark 模式下各主题差异较大，护眼模式如统一可能不协调 | 建议定义统一的 Eye-care 模式令牌集，覆盖所有主题 |
| Q2 | theme-preview 页面是保留独立存在还是完全合并进设置页？ | 独立页面方便开发调试；合并后用户路径更短 | 建议：保留独立页面作为开发者预览工具，设置页内嵌精简版切换器 |
| Q3 | 骨架页面（paper_detail / dft_database / mechanism_knowledge / writing_cards / ai_writer / external_analysis_workbench）是否在本次 UI 重设计中同时补全功能？ | 工作量大幅增加 | 建议：本次仅完成布局骨架 + 主题适配，功能逻辑留后续迭代 |
| Q4 | 主题选择器的交互形式：下拉菜单 / Pill 按钮组 / 弹窗面板？ | 影响导航栏空间和交互体验 | 建议：导航栏右侧放置主题图标按钮，点击弹出浮动面板（类似 theme-preview 的 pill 组） |
| Q5 | 每个页面独立 HTML（当前架构）还是改为 SPA 单页？ | 架构决策，影响导航实现方式 | 当前纯 HTML/CSS/JS 无框架，建议保持多页架构，通过 `shared/` 目录复用组件 |
| Q6 | Dark 模式下 Neumorphism 主题的阴影处理方式？ | Neumorphism 依赖浅色背景产生凸起效果，深色下效果差异大 | 需为 Dark 模式下 Neumorphism 定义特殊的阴影令牌或降级为 Flat 风格 |

---

## 附录 A: 6 种主题风格特征速查

| 主题 | 一句话描述 | 关键视觉特征 |
|------|-----------|-------------|
| **Material** | Google Material Design，紫色调，层叠表面 | 圆角卡片、柔和阴影、Inter + Roboto 字体 |
| **Expressive** | 大胆鲜艳，粉红主色，个性驱动 | 强烈色彩对比、IBM Plex Mono 全系字体、大号标题 |
| **Gradient** | 渐变丰富，紫粉主色，视觉深度 | 渐变背景、Montserrat + Space Grotesk、8pt 基线网格 |
| **Impeccable** | 编辑海报感，琥珀主色，暖调高对比 | 奶油+橙交替色块、Chakra Petch 字体、偏移硬阴影 |
| **Neumorphism** | 柔和凸起，青绿主色，触感拟物 | 同色系内外双阴影、Space Mono 字体、无边界线 |
| **Refined** | 精致极简，蓝色主色，衬线优雅 | Playfair Display 衬线字体、极细阴影、4px 小圆角 |

## 附录 B: 现有代码参考

- **主题令牌实现参考**: `theme-preview/index.html` — 已完整实现 6 主题 + Light/Dark CSS 变量切换
- **设置页结构参考**: `settings/index.html` — 已实现 topnav + section-nav + panel 布局，含 API 配置、IDE 连接提示词、使用说明
- **文献库主布局参考**: `literature_library/index.html` — 双栏布局（侧边栏 380px + 工作区），搜索/筛选/列表/详情
