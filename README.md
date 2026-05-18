# HAE 术后胆漏风险预测

肝泡型包虫病（HAE）根治性肝切除术后胆漏风险评估系统。

## 技术栈

- **后端**: Python FastAPI
- **AI**: Google Gemini Vision API
- **数据库**: MySQL
- **前端**: 纯 HTML/CSS/JS（移动端适配）

## 评分模型

四维评分模型（AUC 0.724）：
| 指标 | 阈值 |
|:-----|:-----|
| ① DBIL | >7.1 μmol/L（1分） |
| ② LDH | >194 U/L（1分） |
| ③ 病灶直径 | >12cm（1分） |
| ④ 大切肝面积 | 是（1分） |

- 总分 ≥2 → 🔴 高风险（胆漏率 86.4%）
- 总分 =1 → 🟡 中风险（胆漏率 9.1%）
- 总分 =0 → 🟢 低风险（胆漏率 4.5%，阴性预测值 96.2%）

## 部署

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置环境变量
export GEMINI_API_KEY=your_key_here
export GEMINI_MODEL=gemini-1.5-flash
export MYSQL_HOST=localhost
export MYSQL_USER=root
export MYSQL_PASSWORD=xxx
export MYSQL_DATABASE=crf_platform

# 3. 建表
mysql -u root -p < hae_schema.sql

# 4. 启动
python bile_leak_api.py
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
