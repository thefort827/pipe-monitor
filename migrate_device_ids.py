"""
设备ID迁移脚本
将数据库中的HSTX_数字格式ID迁移为真实设备ID
"""
import sqlite3
import config

def migrate():
    """迁移设备ID格式"""
    conn = sqlite3.connect(config.DB_PATH)
    cursor = conn.cursor()

    # 获取所有设备
    cursor.execute("SELECT device_id, name FROM devices")
    devices = cursor.fetchall()

    migrated = 0
    for device_id, name in devices:
        # 从name提取真实ID
        if "-" in name:
            real_id = name.split("-")[0]
        else:
            real_id = name

        if real_id and real_id != device_id:
            # 检查目标ID是否已存在
            cursor.execute("SELECT COUNT(*) FROM devices WHERE device_id = ?", (real_id,))
            exists = cursor.fetchone()[0] > 0

            if exists:
                # 目标ID已存在，删除当前记录
                cursor.execute("DELETE FROM devices WHERE device_id = ?", (device_id,))
                print(f"Deleted duplicate: {device_id} (target {real_id} already exists)")
            else:
                # 更新device_id
                cursor.execute(
                    "UPDATE devices SET device_id = ? WHERE device_id = ?",
                    (real_id, device_id)
                )
                print(f"Migrated: {device_id} -> {real_id}")
            migrated += 1

    conn.commit()
    conn.close()
    print(f"\nTotal migrated: {migrated}")

if __name__ == "__main__":
    migrate()
