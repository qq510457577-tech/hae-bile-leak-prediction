-- HAE 胆漏风险预测系统 - SQLite 建表
-- 数据库: SQLite 文件 (由 HAE_DB_PATH 环境变量指定)
-- 迁移日期: 2026-05-25

-- ============================================
-- 1. 患者信息表
-- ============================================
CREATE TABLE IF NOT EXISTS hae_patients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_name TEXT NOT NULL DEFAULT '' COMMENT '患者姓名',
    patient_id TEXT NOT NULL DEFAULT '' COMMENT '患者病历号/身份证号',
    patient_birth_date TEXT DEFAULT '' COMMENT '出生日期，格式YYYY-MM-DD',
    patient_sex TEXT DEFAULT '' COMMENT '性别：男/女/空',
    created_at TEXT DEFAULT (datetime('now','localtime')) COMMENT '记录创建时间'
);
CREATE INDEX IF NOT EXISTS idx_patient_id ON hae_patients(patient_id);
CREATE INDEX IF NOT EXISTS idx_patient_name ON hae_patients(patient_name);

-- ============================================
-- 2. 检查记录表
-- ============================================
CREATE TABLE IF NOT EXISTS hae_examinations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id INTEGER NOT NULL COMMENT '关联hae_patients.id',
    upload_date TEXT DEFAULT (datetime('now','localtime')) COMMENT '上传日期',
    exam_date TEXT DEFAULT '' COMMENT '实际检查日期，格式YYYY-MM-DD',
    series_description TEXT DEFAULT '' COMMENT 'DICOM序列描述',
    modality TEXT DEFAULT 'CT' COMMENT '检查设备类型，默认CT',
    institution TEXT DEFAULT '' COMMENT '检查机构/医院名称',
    zip_filename TEXT DEFAULT '' COMMENT '原始ZIP文件名',
    zip_path TEXT DEFAULT '' COMMENT 'ZIP归档路径',
    zip_expires TEXT DEFAULT NULL COMMENT 'ZIP过期日期（30天后），用于自动清理',
    total_dicom_slices INTEGER DEFAULT 0 COMMENT 'DICOM全序列层数',
    selected_slice_count INTEGER DEFAULT 0 COMMENT '已选精选层面数（通常8张）',
    pixel_spacing TEXT DEFAULT '' COMMENT '像素间距，格式如 0.7mm x 0.7mm',
    status TEXT DEFAULT 'pending' COMMENT '状态：pending待处理/confirmed已确认',
    analysis_json TEXT DEFAULT NULL COMMENT 'Gemini分析结果完整JSON快照',
    FOREIGN KEY (patient_id) REFERENCES hae_patients(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_exam_patient ON hae_examinations(patient_id);
CREATE INDEX IF NOT EXISTS idx_exam_date ON hae_examinations(exam_date);
CREATE INDEX IF NOT EXISTS idx_status ON hae_examinations(status);
CREATE INDEX IF NOT EXISTS idx_upload_date ON hae_examinations(upload_date);

-- ============================================
-- 3. 精选切片表
-- ============================================
CREATE TABLE IF NOT EXISTS hae_selected_slices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    examination_id INTEGER NOT NULL COMMENT '关联hae_examinations.id',
    slice_index INTEGER DEFAULT 0 COMMENT '切片在序列中的排序索引（从0开始）',
    slice_location TEXT DEFAULT '' COMMENT '层面空间位置，如 12.5mm',
    reason TEXT DEFAULT '' COMMENT '选取原因标签：病灶核心/病灶边界/纵向突变/整体覆盖',
    image_path TEXT DEFAULT '' COMMENT 'PNG预览图永久存储路径',
    dicom_path TEXT DEFAULT '' COMMENT '原始DICOM文件永久存储路径',
    FOREIGN KEY (examination_id) REFERENCES hae_examinations(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_slice_exam ON hae_selected_slices(examination_id);

-- ============================================
-- 4. AI分析结果表
-- ============================================
CREATE TABLE IF NOT EXISTS hae_analysis_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    examination_id INTEGER NOT NULL COMMENT '关联hae_examinations.id',
    analysis_type TEXT DEFAULT 'series' COMMENT '分析类型：series多层面/single单图/text文字',
    diameter_gt_12cm TEXT DEFAULT '' COMMENT '病灶直径>12cm？是/否/无法判断',
    diameter_reason TEXT DEFAULT NULL COMMENT '尺寸估算依据',
    large_resection TEXT DEFAULT '' COMMENT '大切肝面积？是/否',
    resection_reason TEXT DEFAULT NULL COMMENT '切除面积判断依据',
    confidence TEXT DEFAULT '' COMMENT '置信度：高/中/低',
    additional_findings TEXT DEFAULT NULL COMMENT '其他影像学发现',
    raw_response TEXT DEFAULT NULL COMMENT 'Gemini原始响应JSON全文',
    disclaimer TEXT DEFAULT NULL COMMENT '免责声明/AI辅助说明',
    created_at TEXT DEFAULT (datetime('now','localtime')) COMMENT '分析时间',
    FOREIGN KEY (examination_id) REFERENCES hae_examinations(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_result_exam ON hae_analysis_results(examination_id);
