#!/usr/bin/env python3
"""
HAE术后胆漏风险预测 - AI影像辅助分析后端
支持上传CT/MRI影像，由视觉大模型分析病灶直径和切除面积
"""

import os
import sys
import json
import uuid
import logging
import re
import zipfile
import io
from pathlib import Path
from datetime import datetime

import uvicorn
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import numpy as np
import pymysql.cursors as pymysql_cursors

# ── 视觉大模型: Gemini ──
try:
    from google import genai
    from google.genai import types as genai_types
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False

# ── Logging ──
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger("bile-leak-api")

# ── Config ──
ALLOWED_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.dcm', '.bmp', '.webp'}
MAX_FILE_SIZE = 20 * 1024 * 1024  # 20MB for single images
MAX_ZIP_SIZE = 300 * 1024 * 1024   # 300MB for DICOM series ZIP
UPLOAD_DIR = Path("/tmp/bile-leak-uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Gemini 配置
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

# 备用：如果你也有 OpenAI 兼容的 key（比如未来想切）
VISION_API_URL = os.getenv("VISION_API_URL", "")
VISION_API_KEY = os.getenv("VISION_API_KEY", "")
VISION_MODEL_OPENAI = os.getenv("VISION_MODEL", "gpt-4o-mini")

# ── Prompt: 医学影像分析 ──
ANALYSIS_PROMPT = """你是一位经验丰富的肝胆外科影像学专家。请分析这张肝脏CT/MRI影像。

影像类型：{image_type}

⚠️ 重要说明：这张图片是单张照片/截图，没有DICOM像素间距(pixel spacing)数据。
你无法精确测量病灶的真实物理尺寸。请仅凭解剖标志粗略估计：
- 肝脏通常上下径约15-18cm，左右径约18-22cm
- 使用"相比肝脏整体大小"来给出"远小于/接近/远大于12cm"的初步印象
- 回答中明确标注"该估算基于肉眼判断，无像素间距校准，仅供参考"

请根据影像特征进行判断：

1. **病灶最大直径是否 > 12cm？**
   - 根据解剖参考粗略估计
   - 回答格式："是" 或 "否" 或 "无法判断"
   - 附上估算依据

2. **是否为大切肝面积手术？**
   - 观察病灶位置：是否累及肝门、是否跨叶、是否靠近主要Glisson鞘
   - 回答格式："是" 或 "否"
   - 附上判断依据

3. **其他可见特征：**
   - 病灶形态、边界、有无钙化/坏死/液化
   - 有无胆管扩张、有无肝内转移
   - 肝脏背景（有无肝硬化、脂肪肝等）

请严格按照以下JSON格式回复（不要额外文字）：
{{
  "diameter_gt_12cm": "是/否/无法判断",
  "diameter_reason": "尺寸估算依据（注明：无像素间距校准）...",
  "large_resection": "是/否",
  "resection_reason": "切除面积判断依据...",
  "additional_findings": "其他影像学发现...",
  "confidence": "高/中/低",
  "disclaimer": "本分析为AI辅助。单张图片无像素间距数据，尺寸为肉眼粗略估计，仅供参考。"
}}
"""

app = FastAPI(title="HAE胆漏AI辅助分析", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Helper: 读取图片为 bytes ──
def read_image_bytes(filepath: str) -> bytes:
    with open(filepath, "rb") as f:
        return f.read()


# ── Helper: 判断文件是否为图片 ──
def is_image(filename: str) -> bool:
    ext = Path(filename).suffix.lower()
    return ext in ALLOWED_EXTENSIONS


# ── Helper: 从 Gemini 响应中提取 JSON ──
def extract_json(text: str) -> dict:
    """从 Gemini 回复中提取 JSON，兼容 markdown 代码块"""
    text = text.strip()
    # 尝试直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 尝试提取 ```json ... ``` 中的内容
    m = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            pass
    # 尝试提取第一个 { ... } 对象
    m = re.search(r'\{.*\}', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return None


# ── 图片脱敏：彻底清除元数据 + 视觉遮挡 ──
def sanitize_medical_image(image_path: str) -> str:
    """
    脱敏策略：
    1. DICOM文件 → 剥离所有患者元数据标签，仅保留像素
    2. 普通图片 → 剥离EXIF/GPS/相机信息
    3. 视觉遮挡四角 + 左右边缘的患者姓名/ID/医院信息
    4. 保留扫描参数区域
    """
    from PIL import Image, ImageDraw
    import io
    
    ext = Path(image_path).suffix.lower()
    
    # ── Step 1: 处理 DICOM (剥离元数据) ──
    if ext == '.dcm':
        try:
            import pydicom
            ds = pydicom.dcmread(image_path)
            # 清除所有患者可识别标签
            patient_tags = [
                'PatientName', 'PatientID', 'PatientBirthDate',
                'PatientBirthTime', 'PatientSex', 'PatientAge',
                'PatientSize', 'PatientWeight', 'PatientAddress',
                'PatientPhoneNumber', 'PatientInsurancePlanCodeSequence',
                'MedicalRecordLocator', 'OtherPatientIDs',
                'OtherPatientNames', 'EthnicGroup', 'Occupation',
                'AdditionalPatientHistory', 'ReferringPhysicianName',
                'ReferringPhysicianAddress', 'ReferringPhysicianTelephoneNumbers',
                'PhysiciansOfRecord', 'NameOfPhysiciansReadingStudy',
                'OperatorsName', 'InstitutionName', 'InstitutionAddress',
                'InstitutionalDepartmentName', 'StationName',
                'DeviceSerialNumber', 'SoftwareVersions',
            ]
            for tag in patient_tags:
                if hasattr(ds, tag):
                    setattr(ds, tag, '')
            
            # 保存匿名化的 DICOM
            anon_path = image_path.replace('.', '_anon.')
            ds.save_as(anon_path)
            
            # 转为 PNG 像素数据做进一步处理
            pixel_array = ds.pixel_array
            # 归一化到 8-bit
            pixel_array = ((pixel_array - pixel_array.min()) / 
                          max(pixel_array.max() - pixel_array.min(), 1) * 255).astype('uint8')
            img = Image.fromarray(pixel_array).convert('RGB')
            log.info(f"DICOM 元数据已清除: {len(patient_tags)} 个患者标签被清空")
        except ImportError:
            log.warning("pydicom 未安装，DICOM 元数据可能未清除")
            img = Image.open(image_path).convert('RGB')
        except Exception as e:
            log.warning(f"DICOM 解析失败: {e}")
            img = Image.open(image_path).convert('RGB')
    else:
        # ── Step 2: 非DICOM → 剥离EXIF ──
        img = Image.open(image_path)
        # 用不带EXIF的方式重新保存（消除GPS、相机型号等）
        data = list(img.getdata())
        img = Image.new(img.mode, img.size)
        img.putdata(data)
        img = img.convert('RGB')
        log.info("EXIF/GPS 元数据已清除")
    
    # ── Step 3: 视觉遮挡四角患者信息 ──
    w, h = img.size
    draw = ImageDraw.Draw(img)
    overlay_color = (20, 24, 28)

    boxes = [
        # 左上角：患者姓名、医院
        (0, 0, int(w * 0.20), int(h * 0.13)),
        # 右上角：患者ID、DOB、检查号
        (int(w * 0.80), 0, w, int(h * 0.13)),
        # 左下角：底部标签
        (0, int(h * 0.90), int(w * 0.20), h),
        # 右下角：底部标签
        (int(w * 0.80), int(h * 0.90), w, h),
        # 左边缘竖排标签
        (0, 0, int(w * 0.05), int(h * 0.35)),
        # 右边缘竖排标签
        (int(w * 0.95), 0, w, int(h * 0.35)),
    ]
    for box in boxes:
        draw.rectangle(box, fill=overlay_color)
    
    # 保存为无元数据的 PNG
    sanitized_path = image_path.replace('.', '_sanitized.')
    img.save(sanitized_path, format='PNG')  # PNG 不含 EXIF
    
    log.info(f"脱敏完成: {w}x{h} -> 元数据已清除 + 视觉遮挡")
    return sanitized_path


# ── 调用 Gemini 视觉模型 ──
async def call_gemini_vision(image_path: str, image_type: str) -> dict:
    """使用 Google Gemini 分析医学影像"""
    if not GEMINI_AVAILABLE:
        raise HTTPException(status_code=500, detail="google-genai SDK 未安装")

    api_key = GEMINI_API_KEY or os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise HTTPException(status_code=400, detail="未配置 GEMINI_API_KEY，请在环境变量或系统服务中设置")

    client = genai.Client(api_key=api_key)
    prompt = ANALYSIS_PROMPT.format(image_type=image_type)
    image_data = read_image_bytes(image_path)
    
    # 确定 MIME 类型
    ext = Path(image_path).suffix.lower()
    mime_map = {
        '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
        '.png': 'image/png', '.bmp': 'image/bmp',
        '.webp': 'image/webp',
    }
    mime = mime_map.get(ext, 'image/jpeg')

    log.info(f"正在分析影像... 模型={GEMINI_MODEL} 大小={len(image_data) // 1024}KB")

    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[
                prompt,
                genai_types.Part.from_bytes(data=image_data, mime_type=mime)
            ],
            config=genai_types.GenerateContentConfig(
                temperature=0.1,
                max_output_tokens=1024,
            )
        )

        if not response.candidates:
            raise HTTPException(status_code=502, detail=f"Gemini 无返回结果: {response}")

        content = response.text
        log.info(f"Gemini 响应长度: {len(content)} 字符")

        # 解析 JSON
        analysis = extract_json(content)
        if not analysis:
            log.warning(f"JSON 解析失败，原始内容: {content[:300]}")
            analysis = {
                "diameter_gt_12cm": "未知",
                "diameter_reason": "AI 响应格式解析失败",
                "large_resection": "未知",
                "resection_reason": "解析失败",
                "additional_findings": content[:200],
                "confidence": "低",
                "disclaimer": "本分析为AI辅助，仅供参考。"
            }

        return analysis

    except Exception as e:
        log.error(f"Gemini API 调用失败: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"Gemini API 调用失败: {str(e)}")


# ── 调用 Gemini 文本模型（免影像分析） ──
async def call_gemini_text(prompt: str) -> str:
    """使用 Gemini 处理纯文本请求"""
    if not GEMINI_AVAILABLE:
        raise HTTPException(status_code=500, detail="google-genai SDK 未安装")

    api_key = GEMINI_API_KEY or os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise HTTPException(status_code=400, detail="未配置 GEMINI_API_KEY")

    client = genai.Client(api_key=api_key)

    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[prompt],
            config=genai_types.GenerateContentConfig(
                temperature=0.2,
                max_output_tokens=1024,
            )
        )
        return response.text
    except Exception as e:
        log.error(f"Gemini 文本调用失败: {e}")
        raise HTTPException(status_code=502, detail=f"Gemini 文本调用失败: {str(e)}")


# ── API: 健康检查 ──
@app.get("/api/bile-leak/health")
async def health():
    return {
        "status": "ok",
        "version": "1.0.0",
        "provider": "gemini",
        "model": GEMINI_MODEL,
        "api_key_configured": bool(GEMINI_API_KEY),
        "timestamp": datetime.now().isoformat()
    }


# ── API: 上传并分析影像 ──
@app.post("/api/bile-leak/analyze")
async def analyze_image(
    file: UploadFile = File(...),
    image_type: str = Form("CT")
):
    """上传影像文件，返回AI辅助分析结果"""

    # 验证文件类型
    if not is_image(file.filename):
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件格式: {file.filename}。支持: jpg, jpeg, png, bmp, webp"
        )

    # 验证文件大小
    content = await file.read()
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=400,
            detail=f"文件过大 ({len(content) // 1024 // 1024}MB)，最大20MB"
        )

    # 保存临时文件
    file_id = uuid.uuid4().hex[:12]
    ext = Path(file.filename).suffix.lower()
    save_path = UPLOAD_DIR / f"{file_id}{ext}"
    with open(save_path, "wb") as f:
        f.write(content)

    log.info(f"收到影像: {file.filename} ({len(content)//1024}KB) -> {save_path}")

    try:
        # 脱敏处理：自动遮挡四角信息
        sanitized_path = sanitize_medical_image(str(save_path))
        
        # 调用 Gemini 视觉模型分析（使用脱敏后的图片）
        analysis = await call_gemini_vision(sanitized_path, image_type)

        # 判断建议的预填值
        suggested_dbil = None  # 影像无法分析DBIL
        suggested_ldh = None   # 影像无法分析LDH
        suggested_size = 1 if analysis.get("diameter_gt_12cm") == "是" else 0
        suggested_resection = 1 if analysis.get("large_resection") == "是" else 0

        result = {
            "success": True,
            "analysis": analysis,
            "suggestions": {
                "dbil": suggested_dbil,
                "ldh": suggested_ldh,
                "size": suggested_size,
                "resection": suggested_resection
            },
            "file_id": file_id,
            "filename": file.filename
        }

        return JSONResponse(result)

    except HTTPException:
        raise
    except Exception as e:
        log.error(f"分析失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"影像分析失败: {str(e)}")

    finally:
        # 清理临时文件
        if save_path.exists():
            save_path.unlink()
        sanitized_try = str(save_path).replace('.', '_sanitized.')
        if Path(sanitized_try).exists():
            Path(sanitized_try).unlink()


# ── API: 病情描述文本辅助分析（支持自动填表）──
@app.post("/api/bile-leak/analyze-text")
async def analyze_text(
    description: str = Form(...)
):
    """根据病情描述文字，辅助评估胆漏风险，返回结构化建议"""
    text_prompt = f"""你是一位肝胆外科医生。根据以下病情描述，评估HAE术后胆漏风险。

患者情况：
{description}

请分析四项风险因素，给出每项的是/否/无法判断，并给出综合建议。

以严格JSON格式回复（不要额外文字）：
{{
  "suggestions": {{
    "dbil": "是/否/无法判断（AI无法仅从描述判断DBIL，留空）",
    "ldh": "是/否/无法判断（AI无法仅从描述判断LDH，留空）",
    "size_gt_12cm": "是/否/无法判断",
    "large_resection": "是/否/无法判断"
  }},
  "diameter_reason": "病灶大小判断依据",
  "resection_reason": "切除面积判断依据",
  "risk_assessment": "总体的风险分层和临床建议",
  "recommendation": "进一步检查或处理建议"
}}"""

    content = await call_gemini_text(text_prompt)

    # 尝试提取结构化JSON
    suggestions = {"dbil": None, "ldh": None, "size": None, "resection": None}
    parsed = extract_json(content)
    if parsed and "suggestions" in parsed:
        s = parsed["suggestions"]
        suggestions = {
            "dbil": None,  # 文字描述无法判断化验指标
            "ldh": None,
            "size": 1 if s.get("size_gt_12cm") == "是" else (0 if s.get("size_gt_12cm") == "否" else None),
            "resection": 1 if s.get("large_resection") == "是" else (0 if s.get("large_resection") == "否" else None),
        }

    return {
        "success": True,
        "analysis": content,
        "suggestions": suggestions,
        "diameter_reason": parsed.get("diameter_reason", "") if parsed else "",
        "resection_reason": parsed.get("resection_reason", "") if parsed else ""
    }


# ═══════════════════════════════════════════
# DICOM 序列批量分析（ZIP上传 → 智能选层 → 多图分析）
# ═══════════════════════════════════════════

def dcm_to_png_bytes(ds) -> bytes:
    """DICOM像素 → 脱敏PNG字节"""
    import io
    from PIL import Image
    # 清除元数据
    for tag in ['PatientName','PatientID','PatientBirthDate',
                 'ReferringPhysicianName','InstitutionName']:
        if hasattr(ds, tag):
            setattr(ds, tag, '')
    # 转8位像素
    arr = ds.pixel_array
    arr = ((arr - arr.min()) / max(arr.max() - arr.min(), 1) * 255).astype('uint8')
    img = Image.fromarray(arr)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return buf.getvalue()


def select_key_slices_from_zip(zip_path: str, max_slices: int = 8) -> dict:
    """
    从ZIP中提取DICOM，智能选择关键层面
    返回: { slices: [{index, slice_location, preview_b64}], work_dir, total }
    """
    import zipfile, shutil, base64
    from PIL import Image
    import io

    job_id = uuid.uuid4().hex[:8]
    work_dir = UPLOAD_DIR / f"series_{job_id}"
    work_dir.mkdir(parents=True, exist_ok=True)

    # 解压
    with zipfile.ZipFile(zip_path, 'r') as z:
        z.extractall(work_dir)

    # 解析所有DICOM
    dcm_data = []  # [(z_pos, filepath, ds, pixel_array)]
    for f in sorted(work_dir.rglob('*')):
        ext = f.suffix.lower()
        if ext in ('.dcm', '') and f.is_file() and f.stat().st_size > 1000:
            try:
                import pydicom
                ds = pydicom.dcmread(str(f), stop_before_pixels=False, force=True)
                if hasattr(ds, 'pixel_array') and ds.pixel_array.size > 0:
                    loc = getattr(ds, 'SliceLocation', None)
                    pos = getattr(ds, 'ImagePositionPatient', None)
                    z_pos = float(pos[2]) if pos else (float(loc) if loc else 0)
                    arr = ds.pixel_array.astype(float)
                    dcm_data.append((z_pos, str(f), ds, arr))
            except:
                pass

    if not dcm_data:
        shutil.rmtree(work_dir)
        return {"error": "未找到有效的DICOM文件", "slices": [], "total": 0}

    # 按空间位置排序
    dcm_data.sort(key=lambda x: x[0])
    total = len(dcm_data)
    
    # 提取所有像素数组（方便纵向对比）
    all_arrays = [d[3] for d in dcm_data]
    all_positions = [d[0] for d in dcm_data]

    # ─── 第一轮：单张层面分析 ───
    # HAE病灶在CT上通常呈低密度（暗区: 10-30 HU vs 肝实质 40-70 HU）
    intra_scores = []  # (intra_score, i, z_pos, path, ds)
    for i, (z_pos, path, ds, arr) in enumerate(dcm_data):
        h, w = arr.shape
        cy, cx = h // 2, w // 2
        roi = arr[cy-h//4:cy+h//4, cx-w//4:cx+w//4]
        if roi.size == 0:
            continue
        
        liver_mean = np.median(roi)
        threshold = liver_mean - 30
        hypo_ratio = np.sum(roi < threshold) / roi.size
        region_std = np.std(roi)
        
        # 层内病灶评分：低密度占比 + 灰度方差
        intra_score = hypo_ratio * 2.0 + region_std / max(liver_mean, 1) * 0.5
        intra_scores.append((intra_score, i, z_pos, path, ds))

    # ─── 第二轮：纵向对比 ───
    # 对每张图像，与前后两张做数值差异分析
    # HAE病灶边界处：纵向像素值会发生突变（正常肝→低密度病灶）
    # 正常组织：纵向过渡平缓
    vertical_scores = {}
    for i in range(1, total - 1):
        prev_arr = all_arrays[i - 1].astype(float)
        curr_arr = all_arrays[i].astype(float)
        next_arr = all_arrays[i + 1].astype(float)
        
        h, w = curr_arr.shape
        cy, cx = h // 2, w // 2
        roi_prev = prev_arr[cy-h//4:cy+h//4, cx-w//4:cx+w//4]
        roi_curr = curr_arr[cy-h//4:cy+h//4, cx-w//4:cx+w//4]
        roi_next = next_arr[cy-h//4:cy+h//4, cx-w//4:cx+w//4]
        
        # 与上一张的差异
        diff_prev = np.abs(roi_curr - roi_prev)
        # 与下一张的差异
        diff_next = np.abs(roi_curr - roi_next)
        # 取两侧差异的平均
        diff_mean = (diff_prev + diff_next) / 2.0
        
        # 纵向变化评分：差异越大 → 越可能是病灶边界
        vertical_score = np.mean(diff_mean) / 10.0  # 归一化到可比较量级
        vertical_scores[i] = vertical_score

    # ─── 综合评分 ───
    scored = []
    for intra_score, i, z_pos, path, ds in intra_scores:
        vert = vertical_scores.get(i, 0)
        # 综合：层内低密度(0.6) + 纵向突变(0.4)
        combined = intra_score * 0.6 + vert * 0.4
        scored.append((combined, i, z_pos, path, ds, intra_score, vert))

    # ─── 智能选层 ───
    scored.sort(key=lambda x: x[0], reverse=True)
    
    selected_set = set()
    selected_slices = []

    # 1. 先取综合评分最高的3张（病灶核心断面 + 边界断面）
    for item in scored[:3]:
        combined, i, z_pos, path, ds, intra, vert = item
        if i not in selected_set:
            selected_set.add(i)
            if intra > vert:
                reason = "病灶核心"
            else:
                reason = "病灶边界"
            selected_slices.append((i, z_pos, path, ds, reason))

    # 2. 再取纵向变化最大的2张（定位病灶起止范围）
    vertical_ranked = sorted(vertical_scores.items(), key=lambda x: x[1], reverse=True)
    for vi, vscore in vertical_ranked:
        if len(selected_slices) >= 5:
            break
        if vi not in selected_set:
            selected_set.add(vi)
            _, z_pos, path, ds = dcm_data[vi][0], dcm_data[vi][1], dcm_data[vi][2], dcm_data[vi][3]
            selected_slices.append((vi, z_pos, path, ds, f"纵向突变{vscore:.1f}"))

    # 3. 均匀覆盖（首/中/尾各1张，看清全肝）
    uniform_indices = [0, total // 2, total - 1]
    for ui in uniform_indices:
        if ui not in selected_set:
            selected_set.add(ui)
            _, z_pos, path, ds = dcm_data[ui][0], dcm_data[ui][1], dcm_data[ui][2], dcm_data[ui][3]
            selected_slices.append((ui, z_pos, path, ds, "整体覆盖"))

    # 4. 如果还不够8张，补剩余综合评分高的
    remaining = [s for s in scored if s[1] not in selected_set]
    for item in remaining:
        if len(selected_slices) >= max_slices:
            break
        combined, i, z_pos, path, ds, intra, vert = item
        if i not in selected_set:
            selected_set.add(i)
            selected_slices.append((i, z_pos, path, ds, f"综合{combined:.2f}"))

    # 按空间位置重新排序（保持解剖顺序）
    selected_slices.sort(key=lambda x: x[1])

    # 保存脱敏PNG + 构建返回数据
    slices = []
    for i, z_pos, path, ds, reason in selected_slices:
        png_bytes = dcm_to_png_bytes(ds)
        b64 = base64.b64encode(png_bytes).decode()

        # 保存脱敏PNG供后续分析
        slice_file = work_dir / f"slice_{i:04d}.png"
        with open(slice_file, 'wb') as f:
            f.write(png_bytes)

        slices.append({
            "index": i,
            "total": total,
            "slice_location": f"{z_pos:.1f}mm" if abs(z_pos) < 1000 else f"#{i+1}",
            "reason": reason,
            "preview_b64": b64,
            "filename": Path(path).name,
            "filepath": str(slice_file),
            "dicom_path": path,  # 原始DICOM文件路径
        })

    log.info(f"DICOM序列: {total}张 -> 选中{len(slices)}张关键层面")

    # 保存元数据文件供后续save-examination使用
    reason_path = work_dir / "slice_reasons.txt"
    reason_path.write_text("\n".join([f"{s['index']}:{s.get('reason','')}" for s in slices]))
    loc_path = work_dir / "slice_locations.txt"
    loc_path.write_text("\n".join([f"{s['index']}:{s.get('slice_location','')}" for s in slices]))
    dcm_path_txt = work_dir / "dicom_paths.txt"
    dcm_path_txt.write_text("\n".join([f"{s['index']}:{s.get('dicom_path','')}" for s in slices]))

    # 提取像素间距（取第一张DICOM的数据）
    pixel_spacing = ""
    if dcm_data:
        first_ds = dcm_data[0][2]
        try:
            ps = first_ds.PixelSpacing
            if ps and len(ps) >= 2:
                pixel_spacing = f"{ps[0]}mm x {ps[1]}mm (row x col)"
        except:
            pass
        if not pixel_spacing:
            try:
                ips = first_ds.ImagerPixelSpacing
                if ips and len(ips) >= 2:
                    pixel_spacing = f"{ips[0]}mm x {ips[1]}mm (row x col, Imager)"
            except:
                pass

    return {
        "job_id": job_id,
        "total": total,
        "slices": slices,
        "work_dir": str(work_dir),
        "pixel_spacing": pixel_spacing
    }


# ── 多层面 Gemini 分析（发送多张图片） ──
async def call_gemini_multi_slice(slice_paths: list, image_type: str, pixel_spacing: str = "") -> dict:
    """发送多个DICOM层面给Gemini分析（含像素间距校准）"""
    api_key = GEMINI_API_KEY or os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise HTTPException(status_code=400, detail="未配置 GEMINI_API_KEY")

    client = genai.Client(api_key=api_key)
    n = len(slice_paths)

    spacing_info = ""
    if pixel_spacing:
        spacing_info = f"\n✅ DICOM像素间距数据: {pixel_spacing}\n请基于此像素间距精确计算病灶的物理尺寸（直径>12cm?）。"

    prompt = f"""你是一位经验丰富的肝胆外科影像学专家。以下是同一患者的连续CT层面图像，共{n}张。{spacing_info}

请综合所有层面，回答以下问题（以JSON格式，不要额外文字）：

1. 病灶最大直径是否 > 12cm？ 回答"是"或"否" + 依据（有像素间距则精确计算）
2. 是否为大切肝面积手术？ 回答"是"或"否" + 依据（观察病灶是否累及肝门、跨叶）
3. 其他影像学发现
4. 置信度：高/中/低

{{
  "diameter_gt_12cm": "是/否",
  "diameter_reason": "...",
  "large_resection": "是/否",
  "resection_reason": "...",
  "additional_findings": "...",
  "confidence": "高/中/低",
  "disclaimer": "本分析为AI辅助，仅供参考。"
}}"""

    # 构建contents：文本 + 多张图片
    contents = [prompt]
    for sp in slice_paths:
        with open(sp, 'rb') as f:
            img_bytes = f.read()
        contents.append(genai_types.Part.from_bytes(data=img_bytes, mime_type='image/png'))

    log.info(f"多层面分析: {n}张图片 -> Gemini ({GEMINI_MODEL})")

    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=contents,
            config=genai_types.GenerateContentConfig(
                temperature=0.1,
                max_output_tokens=1024,
            )
        )
        content = response.text
        analysis = extract_json(content)
        if not analysis:
            analysis = {"error": "解析失败", "raw": content[:300]}
        return analysis
    except Exception as e:
        log.error(f"Gemini多图分析失败: {e}")
        raise HTTPException(status_code=502, detail=f"Gemini分析失败: {str(e)}")


# ── API: 上传ZIP → 提取并预览DICOM序列 ──
@app.post("/api/bile-leak/upload-series")
async def upload_series(file: UploadFile = File(...)):
    """上传DICOM序列ZIP包，返回关键层面预览"""
    if not file.filename.lower().endswith('.zip'):
        raise HTTPException(status_code=400, detail="请上传ZIP文件")

    content = await file.read()
    if len(content) > MAX_ZIP_SIZE:
        raise HTTPException(status_code=400, detail=f"文件过大 (>{MAX_ZIP_SIZE//1024//1024}MB)")

    zip_path = UPLOAD_DIR / f"series_{uuid.uuid4().hex[:12]}.zip"
    with open(zip_path, 'wb') as f:
        f.write(content)

    try:
        result = select_key_slices_from_zip(str(zip_path))
        if "error" in result:
            raise HTTPException(status_code=400, detail=result["error"])

        # 移除完整路径泄露，只保留文件名
        for s in result["slices"]:
            s.pop("filepath", None)
            s.pop("filename", None)

        # 保存像素间距到工作目录，供后续analyze读取
        ps_file = Path(result["work_dir"]) / "pixel_spacing.txt"
        ps_file.write_text(result.get("pixel_spacing", ""))

        return JSONResponse({
            "success": True,
            "job_id": result["job_id"],
            "total_slices": result["total"],
            "selected_slices": result["slices"],
            "analyzed_count": len(result["slices"]),
            "pixel_spacing": result.get("pixel_spacing", "")
        })
    finally:
        if zip_path.exists():
            zip_path.unlink()


# ── API: 分析选中的DICOM层面 ──
@app.post("/api/bile-leak/analyze-series")
async def analyze_series(
    job_id: str = Form(...),
    slice_indices: str = Form(...),  # 逗号分隔的索引，如 "0,2,4,6"
    image_type: str = Form("CT")
):
    """分析选中的DICOM层面"""
    work_dir = UPLOAD_DIR / f"series_{job_id}"
    if not work_dir.exists():
        raise HTTPException(status_code=404, detail="未找到该序列，请重新上传")

    indices = [int(i.strip()) for i in slice_indices.split(',') if i.strip()]
    if not indices:
        raise HTTPException(status_code=400, detail="请至少选择1个层面")

    # 按需选取slice文件
    slice_paths = []
    for idx in indices:
        p = work_dir / f"slice_{idx:04d}.png"
        if p.exists():
            slice_paths.append(str(p))

    if not slice_paths:
        raise HTTPException(status_code=404, detail="未找到对应层面的图片")

    # 读取像素间距数据
    pixel_spacing = ""
    ps_file = work_dir / "pixel_spacing.txt"
    if ps_file.exists():
        pixel_spacing = ps_file.read_text().strip()

    analysis = await call_gemini_multi_slice(slice_paths, image_type, pixel_spacing)

    # 保存分析结果到工作目录，供 save-examination 使用
    analysis_file = work_dir / "analysis_result.json"
    analysis_file.write_text(json.dumps(analysis, ensure_ascii=False, indent=2))

    suggested_size = 1 if analysis.get("diameter_gt_12cm") == "是" else 0
    suggested_resection = 1 if analysis.get("large_resection") == "是" else 0

    return JSONResponse({
        "success": True,
        "analysis": analysis,
        "suggestions": {
            "size": suggested_size,
            "resection": suggested_resection
        },
        "analyzed_slices": len(slice_paths)
    })


# ═══════════════════════════════════════════
# 患者数据管理系统 API
# ═══════════════════════════════════════════

import db as hae_db

ARCHIVE_DIR = Path("/data/bile-leak/archive")
ARCHIVE_ZIP = ARCHIVE_DIR / "zip"
ARCHIVE_SLICES = ARCHIVE_DIR / "slices"
ARCHIVE_DICOM = ARCHIVE_DIR / "dicom"
for d in [ARCHIVE_ZIP, ARCHIVE_SLICES, ARCHIVE_DICOM]:
    d.mkdir(parents=True, exist_ok=True)


@app.post("/api/bile-leak/save-examination")
async def save_examination(
    job_id: str = Form(...),
    patient_name: str = Form(""),
    patient_id_num: str = Form(""),
    patient_birth: str = Form(""),
    patient_sex: str = Form(""),
    series_desc: str = Form(""),
    modality: str = Form("CT"),
    institution: str = Form(""),
    exam_date: str = Form(""),
    total_slices: int = Form(0),
    pixel_spacing: str = Form(""),
    zip_filename: str = Form(""),
):
    """保存确认后的检查记录到数据库，永久存储切片"""
    work_dir = UPLOAD_DIR / f"series_{job_id}"
    if not work_dir.exists():
        raise HTTPException(status_code=404, detail="未找到该序列数据，请重新上传")

    try:
        # 移动ZIP到归档目录（如果存在）
        zip_path = ""
        zip_src = work_dir.parent / f"{job_id}.zip"
        # ZIP在upload-series时已删除，从临时位置找
        zip_candidates = list(UPLOAD_DIR.glob(f"series_{job_id[:12]}*.zip"))
        if zip_candidates:
            zip_src = zip_candidates[0]

        final_zip_path = ""
        if zip_src and zip_src.exists():
            zip_dest = ARCHIVE_ZIP / f"{job_id}.zip"
            shutil.move(str(zip_src), str(zip_dest))
            final_zip_path = str(zip_dest)

        # 移动切片到永久存储（PNG + 原始DICOM）
        slice_records = []
        for f in sorted(work_dir.glob("slice_*.png")):
            dest = ARCHIVE_SLICES / f"{job_id}_{f.name}"
            shutil.copy2(str(f), str(dest))
            # 解析索引
            idx_str = f.stem.replace("slice_", "")
            try:
                idx = int(idx_str)
            except:
                idx = 0
            slice_records.append({
                "index": idx,
                "slice_location": "",
                "reason": "",
                "image_path": str(dest),
                "dicom_path": "",
            })

        # 读取已保存的选择理由
        reason_file = work_dir / "slice_reasons.txt"
        if reason_file.exists():
            reasons = {}
            for line in reason_file.read_text().strip().split("\n"):
                if ":" in line:
                    parts = line.split(":", 1)
                    reasons[int(parts[0])] = parts[1]
            for s in slice_records:
                s["reason"] = reasons.get(s["index"], "")

        # 读取像素间距
        ps = pixel_spacing
        ps_file = work_dir / "pixel_spacing.txt"
        if ps_file.exists():
            ps = ps_file.read_text().strip() or ps

        # 读取slice locations
        loc_file = work_dir / "slice_locations.txt"
        if loc_file.exists():
            locs = {}
            for line in loc_file.read_text().strip().split("\n"):
                if ":" in line:
                    parts = line.split(":", 1)
                    locs[int(parts[0])] = parts[1]
            for s in slice_records:
                s["slice_location"] = locs.get(s["index"], f"#{s['index']+1}")

        # 归档原始DICOM文件到永久存储
        dicom_paths = {}
        dcm_list = work_dir / "dicom_paths.txt"
        if dcm_list.exists():
            for line in dcm_list.read_text().strip().split("\n"):
                if ":" in line:
                    parts = line.split(":", 1)
                    dicom_paths[int(parts[0])] = parts[1]

        # 创建以exam_id命名的DICOM子目录
        # 先用临时ID，等数据库写入后得到真正的exam_id再重命名
        dicom_dir = ARCHIVE_DICOM / f"tmp_{job_id}"
        dicom_dir.mkdir(parents=True, exist_ok=True)

        for s in slice_records:
            src_dcm = dicom_paths.get(s["index"], "")
            if src_dcm and os.path.exists(src_dcm):
                dcm_filename = f"slice_{s['index']:04d}.dcm"
                dest_dcm = dicom_dir / dcm_filename
                shutil.copy2(src_dcm, str(dest_dcm))
                s["dicom_path"] = str(dest_dcm)

        # 读取Gemini分析结果（如果存在）
        analysis = None
        analysis_file = work_dir / "analysis_result.json"
        if analysis_file.exists():
            try:
                analysis = json.loads(analysis_file.read_text())
            except:
                pass

        # 保存到数据库
        exam_id = hae_db.save_examination(
            patient_info={
                "name": patient_name,
                "patient_id": patient_id_num,
                "birth_date": patient_birth,
                "sex": patient_sex,
            },
            exam_info={
                "exam_date": exam_date,
                "series_desc": series_desc,
                "modality": modality,
                "institution": institution,
                "zip_filename": zip_filename,
                "zip_path": final_zip_path,
                "total_slices": total_slices,
                "pixel_spacing": ps,
            },
            slices=slice_records,
            analysis=analysis,
        )

        # 重命名DICOM目录为真实exam_id
        tmp_dicom = ARCHIVE_DICOM / f"tmp_{job_id}"
        if tmp_dicom.exists():
            final_dicom_dir = ARCHIVE_DICOM / str(exam_id)
            shutil.move(str(tmp_dicom), str(final_dicom_dir))
            # 更新数据库中的dicom_path为真实路径
            conn = hae_db.get_conn()
            try:
                with conn.cursor() as cur:
                    for s in slice_records:
                        if s.get("dicom_path"):
                            old_path = s["dicom_path"]
                            new_path = str(final_dicom_dir / f"slice_{s['index']:04d}.dcm")
                            cur.execute(
                                "UPDATE hae_selected_slices SET dicom_path=%s WHERE examination_id=%s AND slice_index=%s",
                                (new_path, exam_id, s["index"])
                            )
                conn.commit()
            finally:
                conn.close()

        return {"success": True, "examination_id": exam_id}

    finally:
        if work_dir.exists():
            shutil.rmtree(work_dir)


@app.post("/api/bile-leak/cancel-series")
async def cancel_series(job_id: str = Form(...)):
    """取消分析，清理工作目录"""
    work_dir = UPLOAD_DIR / f"series_{job_id}"
    deleted = False
    if work_dir.exists():
        import shutil
        shutil.rmtree(work_dir)
        deleted = True
    return {"success": True, "cleaned": deleted}


@app.get("/api/bile-leak/examinations")
async def list_examinations(
    patient_name: str = "",
    patient_id: str = "",
    date_from: str = "",
    date_to: str = "",
    diameter_gt_12cm: str = "",
    large_resection: str = "",
    status: str = "",
    confidence: str = "",
    institution: str = "",
    modality: str = "",
    has_pixel_spacing: str = "",
    keyword: str = "",
):
    """查询检查列表（增强版）"""
    filters = {}
    if patient_name: filters["patient_name"] = patient_name
    if patient_id: filters["patient_id"] = patient_id
    if date_from: filters["date_from"] = date_from
    if date_to: filters["date_to"] = date_to
    if diameter_gt_12cm: filters["diameter_gt_12cm"] = True
    if large_resection: filters["large_resection"] = True
    if status: filters["status"] = status
    if confidence: filters["confidence"] = confidence
    if institution: filters["institution"] = institution
    if modality: filters["modality"] = modality
    if has_pixel_spacing: filters["has_pixel_spacing"] = True
    if keyword: filters["keyword"] = keyword

    exams = hae_db.list_examinations(filters)
    return {"success": True, "examinations": exams, "total": len(exams)}


@app.get("/api/bile-leak/patients/{patient_id}/history")
async def get_patient_history(patient_id: int):
    """获取患者历史检查记录，用于对比"""
    exams = hae_db.get_patient_history(patient_id)
    if not exams:
        raise HTTPException(status_code=404, detail="该患者无检查记录")
    return {"success": True, "examinations": exams, "total": len(exams)}


@app.get("/api/bile-leak/examinations/{exam_id}")
async def get_examination(exam_id: int):
    """获取检查详情"""
    detail = hae_db.get_examination_detail(exam_id)
    if not detail:
        raise HTTPException(status_code=404, detail="未找到该检查记录")
    return {"success": True, "examination": detail}


@app.get("/api/bile-leak/image/{exam_id}/{slice_id}")
async def get_slice_image(exam_id: int, slice_id: int):
    """获取切片图像"""
    conn = hae_db.get_conn()
    try:
        with conn.cursor(pymysql_cursors.DictCursor) as cur:
            cur.execute(
                "SELECT image_path FROM hae_selected_slices WHERE examination_id=%s AND id=%s",
                (exam_id, slice_id)
            )
            row = cur.fetchone()
            if not row or not Path(row["image_path"]).exists():
                raise HTTPException(status_code=404, detail="图像未找到")
            from fastapi.responses import FileResponse
            return FileResponse(row["image_path"], media_type="image/png")
    finally:
        conn.close()


@app.get("/api/bile-leak/dicom-preview/{exam_id}/{slice_id}")
async def get_dicom_preview(exam_id: int, slice_id: int):
    """
    读取原始DICOM，应用合适的窗宽/窗位，渲染为PNG返回
    浏览器可直接显示
    """
    import pydicom
    from PIL import Image
    import io

    conn = hae_db.get_conn()
    try:
        with conn.cursor(pymysql_cursors.DictCursor) as cur:
            cur.execute(
                "SELECT dicom_path FROM hae_selected_slices WHERE examination_id=%s AND id=%s",
                (exam_id, slice_id)
            )
            row = cur.fetchone()
            if not row or not row["dicom_path"] or not Path(row["dicom_path"]).exists():
                # 回退到PNG预览
                cur.execute(
                    "SELECT image_path FROM hae_selected_slices WHERE examination_id=%s AND id=%s",
                    (exam_id, slice_id)
                )
                fallback = cur.fetchone()
                if fallback and Path(fallback["image_path"]).exists():
                    from fastapi.responses import FileResponse
                    return FileResponse(fallback["image_path"], media_type="image/png")
                raise HTTPException(status_code=404, detail="DICOM文件未找到")

            ds = pydicom.dcmread(row["dicom_path"])
            arr = ds.pixel_array.astype(float)

            # 应用窗宽/窗位（肝胆CT默认窗宽400 HU，窗位40 HU）
            wc = float(getattr(ds, 'WindowCenter', 40)) if hasattr(ds, 'WindowCenter') else 40
            ww = float(getattr(ds, 'WindowWidth', 400)) if hasattr(ds, 'WindowWidth') else 400
            if isinstance(wc, (list, tuple)):
                wc = wc[0]
            if isinstance(ww, (list, tuple)):
                ww = ww[0]

            # 窗宽窗位映射
            low = wc - ww / 2.0
            high = wc + ww / 2.0
            arr = (arr - low) / (high - low) * 255.0
            arr = np.clip(arr, 0, 255).astype('uint8')

            from fastapi.responses import StreamingResponse
            img = Image.fromarray(arr).convert('L')
            buf = io.BytesIO()
            img.save(buf, format='PNG')
            buf.seek(0)
            return StreamingResponse(buf, media_type="image/png")

    finally:
        conn.close()


@app.get("/api/bile-leak/dicom-zip/{exam_id}")
async def download_dicom_zip(exam_id: int):
    """打包下载指定检查的所有精选层面原始DICOM文件"""
    conn = hae_db.get_conn()
    try:
        with conn.cursor(pymysql_cursors.DictCursor) as cur:
            cur.execute(
                "SELECT id, slice_index, dicom_path FROM hae_selected_slices "
                "WHERE examination_id=%s AND dicom_path IS NOT NULL AND dicom_path!='' "
                "ORDER BY slice_index",
                (exam_id,)
            )
            slices = cur.fetchall()
            if not slices:
                raise HTTPException(status_code=404, detail="没有可下载的DICOM文件")

        # 获取检查信息做文件名
        conn2 = hae_db.get_conn()
        try:
            with conn2.cursor(pymysql_cursors.DictCursor) as cur2:
                cur2.execute(
                    "SELECT e.id, p.patient_name, e.exam_date FROM hae_examinations e "
                    "JOIN hae_patients p ON e.patient_id=p.id WHERE e.id=%s", (exam_id,)
                )
                exam = cur2.fetchone()
        finally:
            conn2.close()

        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, 'w', zipfile.ZIP_DEFLATED) as zf:
            for s in slices:
                dicom_path = s["dicom_path"]
                p = Path(dicom_path)
                if not p.exists():
                    log.warning(f"DICOM文件缺失: {dicom_path}")
                    continue
                arcname = f"slice_{s['slice_index']:04d}.dcm"
                zf.write(str(p), arcname)

        zip_buf.seek(0)
        exam_date = exam["exam_date"] if exam else "unknown"
        pat_name = exam["patient_name"] if exam else "unknown"
        filename = f"HAE_{exam_id}_{exam_date}_slices.zip"
        from fastapi.responses import Response
        return Response(
            content=zip_buf.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'}
        )
    finally:
        conn.close()


@app.get("/api/bile-leak/dicom-raw/{exam_id}/{slice_id}")
async def get_raw_dicom(exam_id: int, slice_id: int):
    """下载原始DICOM文件"""
    conn = hae_db.get_conn()
    try:
        with conn.cursor(pymysql_cursors.DictCursor) as cur:
            cur.execute(
                "SELECT dicom_path, slice_index FROM hae_selected_slices WHERE examination_id=%s AND id=%s",
                (exam_id, slice_id)
            )
            row = cur.fetchone()
            if not row or not row["dicom_path"] or not Path(row["dicom_path"]).exists():
                raise HTTPException(status_code=404, detail="原始DICOM文件未找到")
            from fastapi.responses import FileResponse
            filename = f"slice_{row['slice_index']:04d}.dcm"
            return FileResponse(row["dicom_path"], media_type="application/dicom",
                               filename=filename,
                               headers={"Content-Disposition": f"attachment; filename={filename}"})
    finally:
        conn.close()


# ── 启动 ──
if __name__ == "__main__":
    port = int(os.getenv("PORT", "3010"))
    provider = "gemini" if GEMINI_API_KEY else "未配置"
    log.info(f"启动胆漏AI辅助分析服务，端口={port}")
    log.info(f"视觉模型: {GEMINI_MODEL} (Gemini)")
    log.info(f"API Key 已配置: {'是' if GEMINI_API_KEY else '否'}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
