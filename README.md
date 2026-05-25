# HAE 术后胆漏风险预测

肝泡型包虫病（HAE）根治性肝切除术后胆漏风险评估系统。  
AI影像辅助分析 + 四维临床评分模型。

## 技术栈

- **后端**: Python FastAPI + uvicorn
- **AI**: Google Gemini Vision API (gemini-1.5-flash)
- **数据库**: SQLite（零配置，文件存储）
- **前端**: 纯 HTML/CSS/JS（移动端适配）

## 评分模型

四维评分模型（AUC 0.724）：

| 指标 | 阈值 | 分值 |
|:-----|:-----|:----:|
| ① DBIL | >7.1 μmol/L | 1 |
| ② LDH | >194 U/L | 1 |
| ③ 病灶直径 | >12cm | 1 |
| ④ 大切肝面积 | 是 | 1 |

- **总分 ≥2** → 🔴 高风险（胆漏率 86.4%）
- **总分 =1** → 🟡 中风险（胆漏率 9.1%）
- **总分 =0** → 🟢 低风险（胆漏率 4.5%，阴性预测值 96.2%）

## 部署

### 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 建表（首次运行）
python db.py         # 自动验证/初始化数据库

# 3. 配置环境变量
export GEMINI_API_KEY=your_key_here
export GEMINI_MODEL=gemini-1.5-flash
export HAE_DB_PATH=/data/bile-leak/crf_platform.db   # SQLite 数据库路径

# 4. 启动
python bile_leak_api.py
```

### 离线或手动建表

```bash
sqlite3 /path/to/crf_platform.db < hae_schema.sql
```

## API 端点

| 端点 | 方法 | 说明 |
|:-----|:-----|:------|
| `/api/bile-leak/analyze` | POST | 单张图片 AI 分析 |
| `/api/bile-leak/analyze-text` | POST | 文字描述 AI 分析 |
| `/api/bile-leak/upload-series` | POST | DICOM ZIP 上传 |
| `/api/bile-leak/analyze-series` | POST | 多层面联合分析 |
| `/api/bile-leak/save-examination` | POST | 保存检查记录 |
| `/api/bile-leak/examinations` | GET | 获取检查列表 |
| `/api/bile-leak/examination/{id}` | GET | 获取检查详情 |

## 隐私说明

- 上传图片自动清除元数据
- 支持纯文字描述模式（零隐私风险）
- DICOM 自动脱敏后上传
- SQLite 本地存储，无需外部数据库服务

## 环境变量

| 变量 | 默认值 | 说明 |
|:-----|:-------|:------|
| `GEMINI_API_KEY` | — | Gemini API 密钥（必填） |
| `GEMINI_MODEL` | `gemini-1.5-flash` | 模型版本 |
| `HAE_DB_PATH` | `./data/crf_platform.db` | SQLite 数据库文件路径 |
| `DATA_DIR` | `/data/bile-leak/archive` | DICOM/ZIP 归档目录 |
