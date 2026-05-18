#!/usr/bin/env python3
"""
HAE胆漏风险预测 - 数据库模块 (MySQL版)
存储患者信息、检查记录、切片数据、分析结果
"""
import os
import json
from pathlib import Path
from datetime import datetime, date, timedelta
import pymysql
from pymysql.cursors import DictCursor

# MySQL 连接配置
MYSQL_CONFIG = {
    "host": os.getenv("MYSQL_HOST", "127.0.0.1"),
    "port": int(os.getenv("MYSQL_PORT", "3306")),
    "user": os.getenv("MYSQL_USER", "crf_user"),
    "password": os.getenv("MYSQL_PASSWORD", "crf_pass_123"),
    "database": os.getenv("MYSQL_DATABASE", "crf_platform"),
    "charset": "utf8mb4",
    "autocommit": False,
}

ARCHIVE_DIR = Path(os.getenv("DATA_DIR", "/data/bile-leak/archive"))
ARCHIVE_ZIP = ARCHIVE_DIR / "zip"
ARCHIVE_SLICES = ARCHIVE_DIR / "slices"


def get_conn():
    """获取 MySQL 连接"""
    conn = pymysql.connect(**MYSQL_CONFIG)
    return conn


def init_db():
    """初始化数据库表 - 已在MySQL中创建，只做验证"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SHOW TABLES LIKE 'hae_%'")
            tables = cur.fetchall()
            expected = {"hae_patients", "hae_examinations", "hae_selected_slices", "hae_analysis_results"}
            actual = {t[0] for t in tables}
            missing = expected - actual
            if missing:
                raise RuntimeError(f"缺失表: {missing}")
            print(f"✅ MySQL 数据库已就绪: {MYSQL_CONFIG['database']} (hae_* 表: {len(actual)})")
    finally:
        conn.close()


def save_examination(patient_info: dict, exam_info: dict, slices: list, analysis: dict = None) -> int:
    """
    保存完整检查记录
    patient_info: {name, patient_id, birth_date, sex}
    exam_info: {exam_date, series_desc, modality, institution, zip_filename, zip_path, total_slices, pixel_spacing}
    slices: [{index, slice_location, reason, image_path}]
    analysis: {diameter_gt_12cm, diameter_reason, large_resection, ...}
    returns: examination_id
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # 1. 查找或创建患者
            cur.execute(
                "SELECT id FROM hae_patients WHERE patient_id=%s AND patient_name=%s",
                (patient_info.get("patient_id", ""), patient_info.get("patient_name", ""))
            )
            row = cur.fetchone()
            if row:
                patient_id = row["id"]
            else:
                cur.execute(
                    "INSERT INTO hae_patients (patient_name, patient_id, patient_birth_date, patient_sex) VALUES (%s,%s,%s,%s)",
                    (patient_info.get("patient_name", ""),
                     patient_info.get("patient_id", ""),
                     patient_info.get("patient_birth_date", ""),
                     patient_info.get("patient_sex", ""))
                )
                patient_id = cur.lastrowid

            # 2. ZIP过期时间（30天）
            zip_expires = (date.today() + timedelta(days=30)).isoformat()

            # 3. 创建检查记录
            cur.execute(
                """INSERT INTO hae_examinations 
                (patient_id, exam_date, series_description, modality, institution,
                 zip_filename, zip_path, zip_expires, total_dicom_slices,
                 selected_slice_count, pixel_spacing, status)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (patient_id,
                 exam_info.get("exam_date", ""),
                 exam_info.get("series_desc", ""),
                 exam_info.get("modality", "CT"),
                 exam_info.get("institution", ""),
                 exam_info.get("zip_filename", ""),
                 exam_info.get("zip_path", ""),
                 zip_expires,
                 exam_info.get("total_slices", 0),
                 len(slices),
                 exam_info.get("pixel_spacing", ""),
                 "confirmed")
            )
            exam_id = cur.lastrowid

            # 4. 保存切片信息
            for s in slices:
                cur.execute(
                    "INSERT INTO hae_selected_slices (examination_id, slice_index, slice_location, reason, image_path, dicom_path) VALUES (%s,%s,%s,%s,%s,%s)",
                    (exam_id, s.get("index", 0), s.get("slice_location", ""),
                     s.get("reason", ""), s.get("image_path", ""), s.get("dicom_path", ""))
                )

            # 5. 保存分析结果
            if analysis:
                cur.execute(
                    """INSERT INTO hae_analysis_results 
                    (examination_id, analysis_type, diameter_gt_12cm, diameter_reason,
                     large_resection, resection_reason, confidence,
                     additional_findings, raw_response, disclaimer)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (exam_id, "series",
                     analysis.get("diameter_gt_12cm", ""),
                     analysis.get("diameter_reason", ""),
                     analysis.get("large_resection", ""),
                     analysis.get("resection_reason", ""),
                     analysis.get("confidence", ""),
                     analysis.get("additional_findings", ""),
                     json.dumps(analysis, ensure_ascii=False),
                     analysis.get("disclaimer", ""))
                )

            # 6. 更新examinations的analysis_json
            if analysis:
                cur.execute(
                    "UPDATE hae_examinations SET analysis_json=%s WHERE id=%s",
                    (json.dumps(analysis, ensure_ascii=False), exam_id)
                )

        conn.commit()
        return exam_id

    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


def list_examinations(filters: dict = None) -> list:
    """查询检查记录列表"""
    conn = get_conn()
    try:
        with conn.cursor(DictCursor) as cur:
            where_clauses = ["1=1"]
            params = []

            if filters:
                if filters.get("patient_name"):
                    where_clauses.append("p.patient_name LIKE %s")
                    params.append(f'%{filters["patient_name"]}%')
                if filters.get("patient_id"):
                    where_clauses.append("p.patient_id LIKE %s")
                    params.append(f'%{filters["patient_id"]}%')
                if filters.get("date_from"):
                    where_clauses.append("e.upload_date >= %s")
                    params.append(filters["date_from"])
                if filters.get("date_to"):
                    where_clauses.append("e.upload_date <= %s")
                    params.append(filters["date_to"] + " 23:59:59")
                if filters.get("diameter_gt_12cm"):
                    where_clauses.append(
                        "JSON_EXTRACT(e.analysis_json, '$.diameter_gt_12cm') = '\"是\"'"
                    )
                if filters.get("large_resection"):
                    where_clauses.append(
                        "JSON_EXTRACT(e.analysis_json, '$.large_resection') = '\"是\"'"
                    )
                if filters.get("status"):
                    where_clauses.append("e.status = %s")
                    params.append(filters["status"])
                if filters.get("confidence"):
                    where_clauses.append(
                        "JSON_EXTRACT(e.analysis_json, '$.confidence') = %s"
                    )
                    params.append(f'"{filters["confidence"]}"')
                if filters.get("institution"):
                    where_clauses.append("e.institution LIKE %s")
                    params.append(f'%{filters["institution"]}%')
                if filters.get("modality"):
                    where_clauses.append("e.modality = %s")
                    params.append(filters["modality"])
                if filters.get("has_pixel_spacing"):
                    where_clauses.append("e.pixel_spacing != ''")
                if filters.get("keyword"):
                    where_clauses.append(
                        "(p.patient_name LIKE %s OR e.series_description LIKE %s OR JSON_EXTRACT(e.analysis_json, '$.additional_findings') LIKE %s)"
                    )
                    kw = f'%{filters["keyword"]}%'
                    params.extend([kw, kw, kw])

            sql = f"""SELECT e.id, e.upload_date, e.exam_date, e.series_description,
                            e.modality, e.total_dicom_slices, e.selected_slice_count,
                            e.status, e.pixel_spacing, e.institution,
                            e.preop_dbil, e.preop_ldh,
                            p.patient_name, p.patient_id, p.patient_birth_date, p.patient_sex,
                            e.analysis_json
                      FROM hae_examinations e
                      JOIN hae_patients p ON e.patient_id = p.id
                      WHERE {' AND '.join(where_clauses)}
                      ORDER BY e.upload_date DESC
                      LIMIT 200"""

            cur.execute(sql, params)
            rows = cur.fetchall()
            result = []
            for r in rows:
                d = dict(r)
                try:
                    d["analysis"] = json.loads(d["analysis_json"]) if d["analysis_json"] else {}
                except (json.JSONDecodeError, TypeError):
                    d["analysis"] = {}
                del d["analysis_json"]
                result.append(d)
            return result

    finally:
        conn.close()


def get_patient_history(patient_id: int) -> list:
    """获取同一患者的所有检查记录（含简要分析摘要），用于历史对比"""
    conn = get_conn()
    try:
        with conn.cursor(DictCursor) as cur:
            cur.execute(
                """SELECT e.id, e.upload_date, e.exam_date, e.series_description,
                          e.modality, e.institution, e.total_dicom_slices,
                          e.selected_slice_count, e.pixel_spacing, e.status,
                          e.analysis_json,
                          p.patient_name, p.patient_id, p.patient_birth_date, p.patient_sex
                   FROM hae_examinations e
                   JOIN hae_patients p ON e.patient_id = p.id
                   WHERE e.patient_id = %s
                   ORDER BY e.exam_date ASC""",
                (patient_id,)
            )
            rows = cur.fetchall()
            result = []
            for r in rows:
                d = dict(r)
                try:
                    d["analysis"] = json.loads(d["analysis_json"]) if d["analysis_json"] else {}
                except:
                    d["analysis"] = {}
                del d["analysis_json"]

                # 每个检查附带切片预览（第一张）
                cur.execute(
                    "SELECT id, slice_index, slice_location, reason, image_path, dicom_path FROM hae_selected_slices WHERE examination_id=%s ORDER BY slice_index LIMIT 1",
                    (d["id"],)
                )
                slice_row = cur.fetchone()
                d["preview_slice"] = dict(slice_row) if slice_row else None

                result.append(d)
            return result
    finally:
        conn.close()


def get_examination_detail(exam_id: int) -> dict:
    """获取单个检查完整信息"""
    conn = get_conn()
    try:
        with conn.cursor(DictCursor) as cur:
            cur.execute(
                """SELECT e.*, p.patient_name, p.patient_id, p.patient_birth_date, p.patient_sex
                   FROM hae_examinations e
                   JOIN hae_patients p ON e.patient_id = p.id
                   WHERE e.id = %s""",
                (exam_id,)
            )
            row = cur.fetchone()
            if not row:
                return None

            result = dict(row)
            try:
                result["analysis"] = json.loads(result["analysis_json"]) if result["analysis_json"] else {}
            except (json.JSONDecodeError, TypeError):
                result["analysis"] = {}
            if "analysis_json" in result:
                del result["analysis_json"]

            # 获取切片
            cur.execute(
                "SELECT id, slice_index, slice_location, reason, image_path, dicom_path FROM hae_selected_slices WHERE examination_id=%s ORDER BY slice_index",
                (exam_id,)
            )
            slices = cur.fetchall()
            result["slices"] = [dict(s) for s in slices]

            # 获取分析结果
            cur.execute(
                "SELECT * FROM hae_analysis_results WHERE examination_id=%s ORDER BY created_at DESC LIMIT 1",
                (exam_id,)
            )
            analysis_row = cur.fetchone()
            result["analysis_result"] = dict(analysis_row) if analysis_row else None

            return result

    finally:
        conn.close()


def delete_old_zips():
    """清理过期ZIP文件（30天以上）"""
    conn = get_conn()
    try:
        with conn.cursor(DictCursor) as cur:
            today = date.today().isoformat()
            cur.execute(
                "SELECT id, zip_path FROM hae_examinations WHERE zip_expires < %s AND zip_path != ''",
                (today,)
            )
            rows = cur.fetchall()

            deleted = 0
            for r in rows:
                zpath = r["zip_path"]
                if zpath and os.path.exists(zpath):
                    os.remove(zpath)
                    deleted += 1
                cur.execute(
                    "UPDATE hae_examinations SET zip_path='', zip_filename='' WHERE id=%s",
                    (r["id"],)
                )

        conn.commit()
        return deleted

    finally:
        conn.close()


# 启动时验证
init_db()

if __name__ == "__main__":
    print(f"数据库: {MYSQL_CONFIG['database']} @ {MYSQL_CONFIG['host']}:{MYSQL_CONFIG['port']}")
    print(f"清理过期ZIP: {delete_old_zips()} 个")
