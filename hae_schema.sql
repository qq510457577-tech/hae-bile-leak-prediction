-- HAE 胆漏风险预测系统 - MySQL 建表（含注释）
-- 数据库: crf_platform

-- ============================================
-- 1. 患者信息表
-- ============================================
CREATE TABLE IF NOT EXISTS hae_patients (
    id INT AUTO_INCREMENT PRIMARY KEY COMMENT '患者内部ID，自增主键',
    patient_name VARCHAR(100) NOT NULL DEFAULT '' COMMENT '患者姓名',
    patient_id VARCHAR(100) NOT NULL DEFAULT '' COMMENT '患者病历号/身份证号',
    patient_birth_date VARCHAR(20) DEFAULT '' COMMENT '出生日期，格式YYYY-MM-DD',
    patient_sex VARCHAR(10) DEFAULT '' COMMENT '性别：男/女/空',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '记录创建时间',
    INDEX idx_patient_id (patient_id) COMMENT '患者ID索引',
    INDEX idx_patient_name (patient_name) COMMENT '患者姓名索引'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='HAE患者基本信息表';

-- ============================================
-- 2. 检查记录表
-- ============================================
CREATE TABLE IF NOT EXISTS hae_examinations (
    id INT AUTO_INCREMENT PRIMARY KEY COMMENT '检查记录ID，自增主键',
    patient_id INT NOT NULL COMMENT '关联hae_patients.id',
    upload_date DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '上传日期',
    exam_date VARCHAR(20) DEFAULT '' COMMENT '实际检查日期，格式YYYY-MM-DD',
    series_description VARCHAR(255) DEFAULT '' COMMENT 'DICOM序列描述',
    modality VARCHAR(20) DEFAULT 'CT' COMMENT '检查设备类型，默认CT',
    institution VARCHAR(200) DEFAULT '' COMMENT '检查机构/医院名称',
    zip_filename VARCHAR(255) DEFAULT '' COMMENT '原始ZIP文件名',
    zip_path VARCHAR(500) DEFAULT '' COMMENT 'ZIP归档路径',
    zip_expires DATE DEFAULT NULL COMMENT 'ZIP过期日期（30天后），用于自动清理',
    total_dicom_slices INT DEFAULT 0 COMMENT 'DICOM全序列层数',
    selected_slice_count INT DEFAULT 0 COMMENT '已选精选层面数（通常8张）',
    pixel_spacing VARCHAR(100) DEFAULT '' COMMENT '像素间距，格式如 0.7mm x 0.7mm',
    status VARCHAR(20) DEFAULT 'pending' COMMENT '状态：pending待处理/confirmed已确认',
    analysis_json JSON DEFAULT NULL COMMENT 'Gemini分析结果完整JSON快照',
    FOREIGN KEY (patient_id) REFERENCES hae_patients(id) ON DELETE CASCADE,
    INDEX idx_exam_patient (patient_id) COMMENT '按患者查询索引',
    INDEX idx_exam_date (exam_date) COMMENT '按检查日期查询索引',
    INDEX idx_status (status) COMMENT '按状态筛选索引',
    INDEX idx_upload_date (upload_date) COMMENT '按上传日期查询索引'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='HAE检查记录表，一次上传对应一条记录';

-- ============================================
-- 3. 精选切片表
-- ============================================
CREATE TABLE IF NOT EXISTS hae_selected_slices (
    id INT AUTO_INCREMENT PRIMARY KEY COMMENT '切片内部ID，自增主键',
    examination_id INT NOT NULL COMMENT '关联hae_examinations.id',
    slice_index INT DEFAULT 0 COMMENT '切片在序列中的排序索引（从0开始）',
    slice_location VARCHAR(100) DEFAULT '' COMMENT '层面空间位置，如 12.5mm',
    reason VARCHAR(200) DEFAULT '' COMMENT '选取原因标签：病灶核心/病灶边界/纵向突变/整体覆盖',
    image_path VARCHAR(500) DEFAULT '' COMMENT 'PNG预览图永久存储路径',
    dicom_path VARCHAR(500) DEFAULT '' COMMENT '原始DICOM文件永久存储路径',
    FOREIGN KEY (examination_id) REFERENCES hae_examinations(id) ON DELETE CASCADE,
    INDEX idx_slice_exam (examination_id) COMMENT '按检查查询切片索引'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='HAE精选切片表，记录智能选层8张切片的元数据和存储路径';

-- ============================================
-- 4. AI分析结果表
-- ============================================
CREATE TABLE IF NOT EXISTS hae_analysis_results (
    id INT AUTO_INCREMENT PRIMARY KEY COMMENT '分析结果ID，自增主键',
    examination_id INT NOT NULL COMMENT '关联hae_examinations.id',
    analysis_type VARCHAR(20) DEFAULT 'series' COMMENT '分析类型：series多层面/single单图/text文字',
    diameter_gt_12cm VARCHAR(20) DEFAULT '' COMMENT '病灶直径>12cm？是/否/无法判断',
    diameter_reason TEXT DEFAULT NULL COMMENT '尺寸估算依据',
    large_resection VARCHAR(20) DEFAULT '' COMMENT '大切肝面积？是/否',
    resection_reason TEXT DEFAULT NULL COMMENT '切除面积判断依据',
    confidence VARCHAR(10) DEFAULT '' COMMENT '置信度：高/中/低',
    additional_findings TEXT DEFAULT NULL COMMENT '其他影像学发现',
    raw_response JSON DEFAULT NULL COMMENT 'Gemini原始响应JSON全文',
    disclaimer TEXT DEFAULT NULL COMMENT '免责声明/AI辅助说明',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '分析时间',
    FOREIGN KEY (examination_id) REFERENCES hae_examinations(id) ON DELETE CASCADE,
    INDEX idx_result_exam (examination_id) COMMENT '按检查查询分析结果索引'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='HAE AI分析结果表，存储Gemini对影像的分析结论';
