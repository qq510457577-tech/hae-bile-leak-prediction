#!/usr/bin/env python3
"""
HAE胆漏风险预测 - 数据库模块 (SQLite版)
存储患者信息、检查记录、切片数据、分析结果
"""
import os
import json
import sqlite3
from pathlib import Path
from datetime import datetime, date, timedelta

DB_PATH = os.getenv("HAE_DB_PATH", "/home/ubuntu/crf-platform/app/data/crf_platform.db")
ARCHIVE_DIR = Path(os.getenv("DATA_DIR", "/data/bile-leak/archive"))
ARCHIVE_ZIP = ARCHIVE_DIR / "zip"
ARCHIVE_SLICES = ARCHIVE_DIR / "slices"


def get_conn():
    """获取 SQLite 连接"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db():
    """验证数据库表存在"""
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'hae_%'")
        tables = {t[0] for t in c.fetchall()}
        expected = {"hae_patients", "hae_examinations", "hae_selected_slices", "hae_analysis_results"}
        missing = expected - tables
        if missing:
            raise RuntimeError(f"缺失表: {missing}")
        print(f"✅ SQLite 数据库已就绪: {DB_PATH} (hae_* 表: {len(tables)})")
    finally:
        conn.close()


def save_examination(patient_info: dict, exam_info: dict, slices: list, analysis: dict = None) -> int:
    """保存完整检查记录"""
    conn = get_conn()
    try:
        c = conn.cursor()
        # 1. 查找或创建患者
        c.execute(
            "SELECT id FROM hae_patients WHERE patient_id=? AND patient_name=?",
            (patient_info.get("patient_id", ""), patient_info.get("patient_name", ""))
        )
        row = c.fetchone()
        if row:
            patient_id = row[0]
        else:
            c.execute(
                "INSERT INTO hae_patients (patient_name, patient_id, patient_birth_date, patient_sex) VALUES (?,?,?,?)",
                (patient_info.get("patient_name", ""),
                 patient_info.get("patient_id", ""),
                 patient_info.get("patient_birth_date", ""),
                 patient_info.get("patient_sex", ""))
            )
            patient_id = c.lastrowid

        # 2. ZIP过期时间
        zip_expires = (date.today() + timedelta(days=30)).isoformat()

        # 3. 创建检查记录
        c.execute(
            """INSERT INTO hae_examinations 
            (patient_id, exam_date, series_description, modality, institution,
             zip_filename, zip_path, zip_expires, total_dicom_slices,
             selected_slice_count, pixel_spacing, status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
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
        exam_id = c.lastrowid

        # 4. 保存切片信息
        for s in slices:
            c.execute(
                "INSERT INTO hae_selected_slices (examination_id, slice_index, slice_location, reason, image_path, dicom_path) VALUES (?,?,?,?,?,?)",
                (exam_id, s.get("index", 0), s.get("slice_location", ""),
                 s.get("reason", ""), s.get("image_path", ""), s.get("dicom_path", ""))
            )

        # 5. 保存分析结果
        if analysis:
            c.execute(
                """INSERT INTO hae_analysis_results 
                (examination_id, analysis_type, diameter_gt_12cm, diameter_reason,
                 large_resection, resection_reason, confidence,
                 additional_findings, raw_response, disclaimer)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
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
            c.execute(
                "UPDATE hae_examinations SET analysis_json=? WHERE id=?",
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
        c = conn.cursor()
        where_clauses = ["1=1"]
        params = []

        if filters:
            if filters.get("patient_name"):
                where_clauses.append("p.patient_name LIKE ?")
                params.append(f'%{filters["patient_name"]}%')
            if filters.get("patient_id"):
                where_clauses.append("p.patient_id LIKE ?")
                params.append(f'%{filters["patient_id"]}%')
            if filters.get("date_from"):
                where_clauses.append("e.upload_date >= ?")
                params.append(filters["date_from"])
            if filters.get("date_to"):
                where_clauses.append("e.upload_date <= ?")
                params.append(filters["date_to"])
            if filters.get("diameter_gt_12cm"):
                where_clauses.append("json_extract(e.analysis_json, '$.diameter_gt_12cm') = '是'")
            if filters.get("large_resection"):
                where_clauses.append("json_extract(e.analysis_json, '$.large_resection') = '是'")
            if filters.get("status"):
                where_clauses.append("e.status = ?")
                params.append(filters["status"])
            if filters.get("confidence"):
                where_clauses.append("json_extract(e.analysis_json, '$.confidence') = ?")
                params.append(filters["confidence"])
            if filters.get("institution"):
                where_clauses.append("e.institution LIKE ?")
                params.append(f'%{filters["institution"]}%')
            if filters.get("modality"):
                where_clauses.append("e.modality = ?")
                params.append(filters["modality"])
            if filters.get("has_pixel_spacing"):
                where_clauses.append("e.pixel_spacing != ''")
            if filters.get("keyword"):
                where_clauses.append(
                    "(p.patient_name LIKE ? OR e.series_description LIKE ? OR json_extract(e.analysis_json, '$.additional_findings') LIKE ?)"
                )
                kw = f'%{filters["keyword"]}%'
                params.extend([kw, kw, kw])

            # DBIL / LDH queries (simplified for SQLite)
            for op, val in filters.get("dbil_conds", []):
                sql_op = {"gt": ">", "gte": ">=", "eq": "=", "lte": "<=", "lt": "<"}.get(op)
                if sql_op:
                    where_clauses.append(f"CAST(e.preop_dbil AS REAL) {sql_op} ?")
                    params.append(float(val))
            for op, val in filters.get("ldh_conds", []):
                sql_op = {"gt": ">", "gte": ">=", "eq": "=", "lte": "<=", "lt": "<"}.get(op)
                if sql_op:
                    where_clauses.append(f"CAST(e.preop_ldh AS REAL) {sql_op} ?")
                    params.append(float(val))

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

        c.execute(sql, params)
        rows = c.fetchall()
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
    """获取同一患者的所有检查记录"""
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute(
            """SELECT e.id, e.upload_date, e.exam_date, e.series_description,
                      e.modality, e.institution, e.total_dicom_slices,
                      e.selected_slice_count, e.pixel_spacing, e.status,
                      e.analysis_json,
                      p.patient_name, p.patient_id, p.patient_birth_date, p.patient_sex
               FROM hae_examinations e
               JOIN hae_patients p ON e.patient_id = p.id
               WHERE e.patient_id = ?
               ORDER BY e.exam_date ASC""",
            (patient_id,)
        )
        rows = c.fetchall()
        result = []
        for r in rows:
            d = dict(r)
            try:
                d["analysis"] = json.loads(d["analysis_json"]) if d["analysis_json"] else {}
            except:
                d["analysis"] = {}
            del d["analysis_json"]

            # 预览切片（第一张）
            c.execute(
                "SELECT id, slice_index, slice_location, reason, image_path, dicom_path FROM hae_selected_slices WHERE examination_id=? ORDER BY slice_index LIMIT 1",
                (d["id"],)
            )
            slice_row = c.fetchone()
            d["preview_slice"] = dict(slice_row) if slice_row else None

            result.append(d)
        return result
    finally:
        conn.close()


def get_examination_detail(exam_id: int) -> dict:
    """获取单个检查完整信息"""
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute(
            """SELECT e.*, p.patient_name, p.patient_id, p.patient_birth_date, p.patient_sex
               FROM hae_examinations e
               JOIN hae_patients p ON e.patient_id = p.id
               WHERE e.id = ?""",
            (exam_id,)
        )
        row = c.fetchone()
        if not row:
            return None

        result = dict(row)
        try:
            result["analysis"] = json.loads(result["analysis_json"]) if result["analysis_json"] else {}
        except:
            result["analysis"] = {}
        if "analysis_json" in result:
            del result["analysis_json"]

        # 获取切片
        c.execute(
            "SELECT id, slice_index, slice_location, reason, image_path, dicom_path FROM hae_selected_slices WHERE examination_id=? ORDER BY slice_index",
            (exam_id,)
        )
        result["slices"] = [dict(s) for s in c.fetchall()]

        # 获取分析结果
        c.execute(
            "SELECT * FROM hae_analysis_results WHERE examination_id=? ORDER BY created_at DESC LIMIT 1",
            (exam_id,)
        )
        analysis_row = c.fetchone()
        result["analysis_result"] = dict(analysis_row) if analysis_row else None

        return result

    finally:
        conn.close()


def delete_old_zips():
    """清理过期ZIP文件（30天以上）"""
    conn = get_conn()
    try:
        c = conn.cursor()
        today = date.today().isoformat()
        c.execute(
            "SELECT id, zip_path FROM hae_examinations WHERE zip_expires < ? AND zip_path != ''",
            (today,)
        )
        rows = c.fetchall()

        deleted = 0
        for r in rows:
            zpath = r["zip_path"]
            if zpath and os.path.exists(zpath):
                os.remove(zpath)
                deleted += 1
            c.execute(
                "UPDATE hae_examinations SET zip_path='', zip_filename='' WHERE id=?",
                (r["id"],)
            )

        conn.commit()
        return deleted

    finally:
        conn.close()


# 启动时验证
init_db()

if __name__ == "__main__":
    print(f"SQLite 数据库: {DB_PATH}")
    print(f"清理过期ZIP: {delete_old_zips()} 个")
