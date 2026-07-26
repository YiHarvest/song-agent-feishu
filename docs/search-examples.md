# 搜索功能使用示例

## 基本用法

### 1. 搜索最新信息

```
用户：搜索最新的 AI 新闻
Agent：正在搜索...

搜索结果（you）：

1. **OpenAI 发布 GPT-5**
   OpenAI 今日发布了最新的 GPT-5 模型，性能提升显著...
   [链接](https://example.com/news/1)

2. **Google 推出 Gemini 2.0**
   Google 宣布推出新一代 AI 模型 Gemini 2.0...
   [链接](https://example.com/news/2)
```

### 2. 查找技术文档

```
用户：查找 Python 异步编程最佳实践
Agent：正在搜索...

搜索结果（you）：

1. **Python 异步编程完全指南**
   本文详细介绍 Python asyncio 的使用方法和最佳实践...
   [链接](https://example.com/docs/async)

2. **异步编程常见陷阱**
   在使用 Python 异步编程时，需要注意以下常见陷阱...
   [链接](https://example.com/docs/traps)
```

### 3. 查询产品信息

```
用户：查询 React 19 新特性
Agent：正在搜索...

搜索结果（you）：

1. **React 19 正式发布**
   React 19 带来了多项重大更新，包括新的并发特性...
   [链接](https://example.com/react19)

2. **React 19 迁移指南**
   从 React 18 升级到 React 19 的完整迁移指南...
   [链接](https://example.com/migration)
```

## 高级用法

### 1. 指定搜索引擎

```
用户：用 Tavily 搜索最新的机器学习论文
Agent：正在使用 Tavily 搜索...

搜索结果（tavily）：

1. **Attention Is All You Need**
   经典的 Transformer 论文，奠定了现代 NLP 的基础...
   [链接](https://arxiv.org/abs/1706.03762)
```

### 2. 限制结果数量

```
用户：搜索 Python 教程，只显示前 3 个结果
Agent：正在搜索...

搜索结果（you）：

1. **Python 官方教程**
   Python 官方提供的入门教程...
   [链接](https://docs.python.org/tutorial)

2. **Python 快速入门**
   30 分钟快速学会 Python 基础...
   [链接](https://example.com/quickstart)

3. **Python 进阶指南**
   适合有基础的开发者进阶学习...
   [链接](https://example.com/advanced)
```

## 结合其他功能

### 1. 搜索后创建文档

```
用户：搜索 Python 异步编程，然后整理成文档
Agent：正在搜索...

[搜索结果]

正在创建文档...
文档已创建：[Python 异步编程笔记](https://feishu.cn/docx/xxx)
```

### 2. 搜索后添加到计划

```
用户：搜索今天的科技新闻，然后添加到今天的计划中
Agent：正在搜索...

[搜索结果]

已添加到今日计划：
- A1: 阅读 AI 新闻（灵活安排）
- A2: 了解 React 19 新特性（灵活安排）
```

## 注意事项

1. **搜索关键词**：使用具体的关键词可以获得更准确的结果
2. **结果数量**：默认返回 5 个结果，可以指定 1-10 个
3. **搜索引擎**：You.com 和 Tavily 可能有不同的结果，可以尝试两个
4. **网络连接**：搜索需要网络连接，请确保网络畅通
5. **API 限制**：注意 API 调用频率限制，避免过度使用

## 错误处理

### 1. 未找到结果

```
用户：搜索一个不存在的关键词 xyz123abc
Agent：未找到相关结果
```

### 2. 搜索失败

```
用户：搜索测试
Agent：搜索失败：网络连接超时，请稍后重试
```

### 3. 未配置 API Key

```
用户：搜索测试
Agent：搜索失败：未配置搜索引擎 API Key
```