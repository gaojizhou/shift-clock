from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AuditMixin:
    """统一审计 + 软删除字段：新建/修改时间、新建人、修改人、软删除标记。"""

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)


class AppSession(Base):
    """持久化登录会话：浏览器 cookie 只保存 session_id，身份映射存数据库。"""

    __tablename__ = "app_session"

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    userid: Mapped[str] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(32))
    is_super: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class AttendanceResyncJob(Base):
    __tablename__ = "attendance_resync_job"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    schedule_key: Mapped[str | None] = mapped_column(String(32), unique=True, nullable=True)
    actor: Mapped[str] = mapped_column(String(64))
    region: Mapped[str | None] = mapped_column(String(128), nullable=True)
    date_from: Mapped[str] = mapped_column(String(10))
    date_to: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AttendanceResyncItem(Base):
    __tablename__ = "attendance_resync_item"
    __table_args__ = (UniqueConstraint("job_id", "emp_id", "date"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("attendance_resync_job.id"), index=True)
    emp_id: Mapped[str] = mapped_column(String(64))
    date: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    before_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    after_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class Role(AuditMixin, Base):
    """角色字典：操作工/值班长/人事专员/统计员/系统管理员/维修工/班组长。code 即业务唯一ID。"""

    __tablename__ = "role"

    code: Mapped[str] = mapped_column(String(16), primary_key=True)  # worker/chief/hr/stat/admin
    name: Mapped[str] = mapped_column(String(32))
    sort: Mapped[int] = mapped_column(Integer, default=0)


class Position(AuditMixin, Base):
    """职位字典（值班长/维修工…）。code 带前缀 POS。"""

    __tablename__ = "position"

    code: Mapped[str] = mapped_column(String(16), primary_key=True)  # 如 POS01
    name: Mapped[str] = mapped_column(String(64), unique=True)
    sort: Mapped[int] = mapped_column(Integer, default=0)


class Region(AuditMixin, Base):
    """地区字典。code 带前缀 REG。"""

    __tablename__ = "region"

    code: Mapped[str] = mapped_column(String(16), primary_key=True)  # 如 REG01
    name: Mapped[str] = mapped_column(String(64), unique=True)
    sort: Mapped[int] = mapped_column(Integer, default=0)


class Squad(AuditMixin, Base):
    """班组字典（1/2/3班）。code 带前缀 SQ。"""

    __tablename__ = "squad"

    code: Mapped[str] = mapped_column(String(16), primary_key=True)  # 如 SQ1
    name: Mapped[str] = mapped_column(String(16), unique=True)
    sort: Mapped[int] = mapped_column(Integer, default=0)


class FurnaceType(AuditMixin, Base):
    """设备类型字典（标准型A / 标准型B…）。code 带前缀 FT；工作点/编制模板仅存 code 引用。"""

    __tablename__ = "furnace_type"

    code: Mapped[str] = mapped_column(String(16), primary_key=True)  # 如 FT01
    name: Mapped[str] = mapped_column(String(64), unique=True)
    sort: Mapped[int] = mapped_column(Integer, default=0)


class SquadSchedule(AuditMixin, Base):
    """时段时间表：每地区早/中/晚时段的起止时间（HH:MM）。end<=start 表示跨午夜。"""

    __tablename__ = "squad_schedule"

    id: Mapped[int] = mapped_column(primary_key=True)
    region: Mapped[str] = mapped_column(String(64))
    region_code: Mapped[str | None] = mapped_column(ForeignKey("region.code"), nullable=True)
    squad: Mapped[str] = mapped_column(String(16))  # 早班/中班/晚班时段
    squad_code: Mapped[str | None] = mapped_column(ForeignKey("squad.code"), nullable=True)
    start_time: Mapped[str] = mapped_column(String(5))  # "02:00"
    end_time: Mapped[str] = mapped_column(String(5))  # "10:00"；<=start 即跨午夜

    __table_args__ = (UniqueConstraint("region", "squad"),)


class Furnace(Base):
    __tablename__ = "furnace"

    id: Mapped[int] = mapped_column(primary_key=True)
    # 带前缀全局唯一编码（如 FUR01）：A 地区 1 号工作点与 B 地区 1 号工作点编码不同。
    code: Mapped[str | None] = mapped_column(String(16), unique=True, nullable=True)
    region: Mapped[str] = mapped_column(String(64))
    region_code: Mapped[str | None] = mapped_column(ForeignKey("region.code"), nullable=True)
    furnace_no: Mapped[str] = mapped_column(String(32))
    # 纯数字排序值（取自工作点编号，如 “1号工作点”→1、“35号工作点”→35），列表按它排序
    furnace_no_order: Mapped[int] = mapped_column(Integer, default=0)
    # 设备类型仅存字典 code（FTxx）；名称展示一律 join furnace_type 字典
    furnace_type_code: Mapped[str | None] = mapped_column(
        ForeignKey("furnace_type.code"), nullable=True
    )
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    chief_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active")

    quota_rules: Mapped[list["QuotaRule"]] = relationship(back_populates="furnace")

    __table_args__ = (UniqueConstraint("region", "furnace_no"),)


class QuotaRule(Base):
    __tablename__ = "quota_rule"

    id: Mapped[int] = mapped_column(primary_key=True)
    furnace_id: Mapped[int] = mapped_column(ForeignKey("furnace.id"))
    squad: Mapped[str | None] = mapped_column(String(16), nullable=True)
    squad_code: Mapped[str | None] = mapped_column(ForeignKey("squad.code"), nullable=True)
    post: Mapped[str] = mapped_column(String(64))
    position_code: Mapped[str | None] = mapped_column(ForeignKey("position.code"), nullable=True)
    capacity: Mapped[int] = mapped_column(Integer)

    furnace: Mapped[Furnace] = relationship(back_populates="quota_rules")


class PostCatalog(Base):
    """岗位字典：编制编辑里岗位下拉的可选项（HR 在「岗位字典」子页维护）。"""

    __tablename__ = "post_catalog"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)  # 标记删除


class FurnaceChief(AuditMixin, Base):
    """一工作点多值班长：工作点↔值班长 多对多。值班长在日确认可见该工作点所有值班长并对编制有操作权。"""

    __tablename__ = "furnace_chief"

    id: Mapped[int] = mapped_column(primary_key=True)
    furnace_id: Mapped[int] = mapped_column(ForeignKey("furnace.id"))
    emp_id: Mapped[str] = mapped_column(String(32))

    __table_args__ = (UniqueConstraint("furnace_id", "emp_id"),)


class ChiefWorker(Base):
    """值班长-常用员工映射：值班长代签时的常用员工；也即员工绑定到值班长。HR/值班长可维护。"""

    __tablename__ = "chief_worker"

    id: Mapped[int] = mapped_column(primary_key=True)
    chief_user_id: Mapped[str] = mapped_column(String(64))
    emp_id: Mapped[str] = mapped_column(String(64))

    __table_args__ = (UniqueConstraint("chief_user_id", "emp_id"),)


class EmployeeFavorite(Base):
    """员工常用关联：kind ∈ {post 职位, furnace 工作点, region 地区}。HR 在员工管理维护。"""

    __tablename__ = "employee_favorite"

    id: Mapped[int] = mapped_column(primary_key=True)
    emp_id: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(16))
    value: Mapped[str] = mapped_column(String(64))

    __table_args__ = (UniqueConstraint("emp_id", "kind", "value"),)


class QuotaTemplate(Base):
    """按设备类型的编制模板：squadless 岗位(值班长/维修工)squad=None；分班岗位三班同编制，模板只存单班值。"""

    __tablename__ = "quota_template"

    id: Mapped[int] = mapped_column(primary_key=True)
    region: Mapped[str | None] = mapped_column(String(64), nullable=True)  # None=全局通用
    region_code: Mapped[str | None] = mapped_column(ForeignKey("region.code"), nullable=True)
    # 设备类型仅存字典 code（FTxx）
    furnace_type_code: Mapped[str | None] = mapped_column(
        ForeignKey("furnace_type.code"), nullable=True
    )
    post: Mapped[str] = mapped_column(String(64))
    position_code: Mapped[str | None] = mapped_column(ForeignKey("position.code"), nullable=True)
    # None=不分班(值班长/维修工)；否则按班，三班同值故仅存单班标识
    squad: Mapped[str | None] = mapped_column(String(16), nullable=True)
    squad_code: Mapped[str | None] = mapped_column(ForeignKey("squad.code"), nullable=True)
    capacity: Mapped[int] = mapped_column(Integer)  # 早班（不分班岗即该岗人数）
    # 中/晚班人数；None 表示与早班联动（同编制）
    capacity2: Mapped[int | None] = mapped_column(Integer, nullable=True)
    capacity3: Mapped[int | None] = mapped_column(Integer, nullable=True)
    squadless: Mapped[bool] = mapped_column(Boolean, default=False)
    # 可见角色：role.code 逗号拼接（如 "worker" / "worker,leader"）。扫码时该组角色才能看到并选择此岗。
    # 空串=无人可扫码选。默认：分班岗→"worker"、不分班岗→"chief"。
    visible_roles: Mapped[str] = mapped_column(String(128), default="", server_default="")

    __table_args__ = (
        UniqueConstraint("region", "furnace_type_code", "post", "squad"),
    )


class QuotaTemplateScope(Base):
    """地区设备类型模板已初始化标记；即使模板被有意清空也阻止回退到全局模板。"""

    __tablename__ = "quota_template_scope"

    id: Mapped[int] = mapped_column(primary_key=True)
    region: Mapped[str] = mapped_column(String(64))
    region_code: Mapped[str | None] = mapped_column(ForeignKey("region.code"), nullable=True)
    furnace_type_code: Mapped[str] = mapped_column(ForeignKey("furnace_type.code"))

    __table_args__ = (UniqueConstraint("region", "furnace_type_code"),)


class SelectablePost(Base):
    """Worker-selectable posts whitelist (region-wide or per-furnace override)."""

    __tablename__ = "selectable_post"

    id: Mapped[int] = mapped_column(primary_key=True)
    region: Mapped[str] = mapped_column(String(64))
    region_code: Mapped[str | None] = mapped_column(ForeignKey("region.code"), nullable=True)
    furnace_no: Mapped[str | None] = mapped_column(String(32), nullable=True)
    post: Mapped[str] = mapped_column(String(64))
    position_code: Mapped[str | None] = mapped_column(ForeignKey("position.code"), nullable=True)
    # 可见角色：role.code 逗号拼接；扫码时该组角色才能看到并选择此岗（空=无人可选）。
    visible_roles: Mapped[str] = mapped_column(String(128), default="worker", server_default="worker")

    __table_args__ = (UniqueConstraint("region", "furnace_no", "post"),)


class Employee(AuditMixin, Base):
    __tablename__ = "employee"

    id: Mapped[int] = mapped_column(primary_key=True)
    emp_id: Mapped[str] = mapped_column(String(32), unique=True)  # 工号=业务唯一ID
    name: Mapped[str] = mapped_column(String(64))
    id_card: Mapped[str | None] = mapped_column(String(32), nullable=True)
    dd_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    role: Mapped[str | None] = mapped_column(String(16), nullable=True)  # → role.code
    sync_status: Mapped[str] = mapped_column(String(32), default="mock")
    # 人事在职状态与 deleted 软删除分离：通讯录返回 active=False 时只标记离职，
    # 不删除员工、历史出勤、收藏或工作点关联；后续 active=True 可自动恢复在职。
    employment_status: Mapped[str] = mapped_column(
        String(16), default="active", server_default="active"
    )
    # 是否允许钉钉推送：所有推送发送前必须判断为 True 才发。存量/手动同步默认 False，
    # 自助导入(import-self)后置 True；可由本人(h5设置)或 admin(用户管理)开关。
    push_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # HR/统计首次确认页面展示地区的时间；NULL 表示仍须完成首次选区。
    # 地区值复用 employee_favorite(kind=region)，确认后仅 admin 可调整。
    region_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AttendanceRecord(Base):
    """统一工时记录表（出勤/加班/补录代签），替代 ScanRecord + MakeupRequest。

    三轴区分：record_kind(出勤/加班) × source(扫码/代签/补录) × status(待审/active/拒绝/已取消)。
    工时口径 work_hours：整班=8.0，半班=4.0（占比制 1.0/0.5 的工时制替换）。
    加班维度(record_kind/region_code/settled_hours/recon_status)已预留，加班逻辑后续迭代。
    """

    __tablename__ = "attendance_record"

    id: Mapped[int] = mapped_column(primary_key=True)
    emp_id: Mapped[str] = mapped_column(String(32))
    furnace_id: Mapped[int] = mapped_column(ForeignKey("furnace.id"))
    squad: Mapped[str | None] = mapped_column(String(16), nullable=True)
    squad_code: Mapped[str | None] = mapped_column(ForeignKey("squad.code"), nullable=True)
    # 加班时段：仅 record_kind=加班 使用，取值为早班/中班/晚班；基础出勤为 NULL。
    shift_slot: Mapped[str | None] = mapped_column(String(16), nullable=True)
    post: Mapped[str] = mapped_column(String(64))
    position_code: Mapped[str | None] = mapped_column(ForeignKey("position.code"), nullable=True)
    employ_type: Mapped[str] = mapped_column(String(16))
    region_code: Mapped[str | None] = mapped_column(ForeignKey("region.code"), nullable=True)
    scan_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="active")
    natual_date: Mapped[str] = mapped_column(String(10))
    # 出勤工时（算薪占用口径）：整班=8.0，半班=4.0。半班由值班长在日结侧标注。
    work_hours: Mapped[float] = mapped_column(Float, default=8.0, server_default="8")
    # 记录类型：出勤 / 加班
    record_kind: Mapped[str] = mapped_column(String(16), default="出勤", server_default="出勤")
    # 历史字段：统一按 true 处理，值班长端不再允许控制是否核对钉钉考勤。
    recon_dingtalk: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    # 延时加班对账核定后工时；出勤/班内 = work_hours
    settled_hours: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 延时加班对账状态：待拉数据/已覆盖/缺下班卡/已补卡
    recon_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # 申请原因（补录/代签自由文本）
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 审批人工号（补录/代签审批，或加班值班长前置确认）
    reviewed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 迁移溯源
    legacy_table: Mapped[str | None] = mapped_column(String(32), nullable=True)
    legacy_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class ScanRecord(Base):
    """DEPRECATED 2026-06-19：已迁至 AttendanceRecord（工时制），仅留存历史与回滚。"""

    __tablename__ = "scan_record"

    id: Mapped[int] = mapped_column(primary_key=True)
    emp_id: Mapped[str] = mapped_column(String(32))
    furnace_id: Mapped[int] = mapped_column(ForeignKey("furnace.id"))
    squad: Mapped[str | None] = mapped_column(String(16), nullable=True)
    squad_code: Mapped[str | None] = mapped_column(ForeignKey("squad.code"), nullable=True)
    post: Mapped[str] = mapped_column(String(64))
    position_code: Mapped[str | None] = mapped_column(ForeignKey("position.code"), nullable=True)
    employ_type: Mapped[str] = mapped_column(String(16))
    scan_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="active")
    natual_date: Mapped[str] = mapped_column(String(10))
    # 出勤时长占比（算薪占用口径）：整天=1.0，半天=0.5。半天由值班长在日结侧标注。
    day_fraction: Mapped[float] = mapped_column(Float, default=1.0, server_default="1.0")


class MakeupRequest(Base):
    """DEPRECATED 2026-06-19：已并入 AttendanceRecord（source=补录/代签），仅留存历史与回滚。"""

    __tablename__ = "makeup_request"

    id: Mapped[int] = mapped_column(primary_key=True)
    emp_id: Mapped[str] = mapped_column(String(32))
    furnace_id: Mapped[int] = mapped_column(ForeignKey("furnace.id"))
    squad: Mapped[str | None] = mapped_column(String(16), nullable=True)
    squad_code: Mapped[str | None] = mapped_column(ForeignKey("squad.code"), nullable=True)
    post: Mapped[str] = mapped_column(String(64))
    position_code: Mapped[str | None] = mapped_column(ForeignKey("position.code"), nullable=True)
    employ_type: Mapped[str] = mapped_column(String(16), default="正式")
    natual_date: Mapped[str] = mapped_column(String(10))
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="待审")
    approver: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # "代签" (chief proxy sign-in, capped per worker/month) | "补录" (worker-initiated makeup)
    kind: Mapped[str] = mapped_column(String(16), default="补录")
    # 出勤时长占比：整班=1.0，半班=0.5（与 ScanRecord.day_fraction 同口径）
    day_fraction: Mapped[float] = mapped_column(Float, default=1.0, server_default="1.0")


class LaoqinPunch(Base):
    __tablename__ = "laoqin_punch"

    id: Mapped[int] = mapped_column(primary_key=True)
    emp_id: Mapped[str] = mapped_column(String(32))
    org_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    punch_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    inout_type: Mapped[str] = mapped_column(String(16))
    record_source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 补卡原因（src=工作流 时，从 detail.reason 映射的可读文案；非补卡为空）
    makeup_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    natual_date: Mapped[str] = mapped_column(String(10))
    raw_name: Mapped[str | None] = mapped_column(String(64), nullable=True)


class DingtalkAttendanceRaw(Base):
    """钉钉考勤打卡原始流水（从外部 MySQL 视图 view_external_att_data 同步而来）。
    原样镜像源字段，业务键(code, att_time, direct, src)幂等去重；再由「转换」生成 laoqin_punch。"""

    __tablename__ = "dingtalk_attendance_raw"
    __table_args__ = (
        UniqueConstraint(
            "code", "att_time", "direct", "src", name="uq_dingtalk_att_raw_bizkey"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    code: Mapped[str] = mapped_column(String(50))  # 工号（=钉钉 userid）
    name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    org: Mapped[str | None] = mapped_column(String(64), nullable=True)  # 末级组织（工作点编号）
    layer: Mapped[str | None] = mapped_column(String(1000), nullable=True)  # 完整层级路径
    att_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # 打卡时刻
    # 32 而非 8：SQL_ASCII 库按字节计长，'自动判断'=12 字节会撑爆 varchar(8)（见迁移 0056）
    direct: Mapped[str] = mapped_column(String(32), default="")  # 方向（进/出/自动判断）
    state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    src: Mapped[str] = mapped_column(String(16), default="")  # 来源（钉钉打卡）
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON：打卡机地址等
    natual_date: Mapped[str] = mapped_column(String(10))  # 归属自然日 date(att_time)
    src_create_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    src_modify_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ExternalAttendanceResultRaw(Base):
    """外部考勤结果（日结果视图 view_external_att_result）。

    原样镜像源视图，按 user_code + kq_date 幂等覆盖；用于和原始打卡流水分开查看。
    """

    __tablename__ = "external_attendance_result_raw"
    __table_args__ = (UniqueConstraint("user_code", "kq_date", name="uq_external_att_res_user_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_code: Mapped[str | None] = mapped_column(String(255), nullable=True)
    dept_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    layer_name: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    hr_status: Mapped[str | None] = mapped_column(String(255), nullable=True)
    hr_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    user_code: Mapped[str] = mapped_column(String(255))
    user_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    kq_date_show: Mapped[str | None] = mapped_column(String(255), nullable=True)
    kq_date_title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    kq_date: Mapped[str] = mapped_column(String(10))
    kqqs: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    lx_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    actual_on: Mapped[str | None] = mapped_column(String(255), nullable=True)
    actual_off: Mapped[str | None] = mapped_column(String(255), nullable=True)
    quota: Mapped[str | None] = mapped_column(String(255), nullable=True)
    cqgs: Mapped[str | None] = mapped_column(String(255), nullable=True)
    kggs: Mapped[str | None] = mapped_column(String(255), nullable=True)
    cd2: Mapped[str | None] = mapped_column(String(255), nullable=True)
    zt2: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ot1: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ot2: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ot3: Mapped[str | None] = mapped_column(String(255), nullable=True)
    leave_01: Mapped[str | None] = mapped_column(String(255), nullable=True)
    leave_02: Mapped[str | None] = mapped_column(String(255), nullable=True)
    leave_03: Mapped[str | None] = mapped_column(String(255), nullable=True)
    leave_04: Mapped[str | None] = mapped_column(String(255), nullable=True)
    leave_05: Mapped[str | None] = mapped_column(String(255), nullable=True)
    leave_06: Mapped[str | None] = mapped_column(String(255), nullable=True)
    leave_07: Mapped[str | None] = mapped_column(String(255), nullable=True)
    leave_09: Mapped[str | None] = mapped_column(String(255), nullable=True)
    leave_10: Mapped[str | None] = mapped_column(String(255), nullable=True)
    leave_12: Mapped[str | None] = mapped_column(String(255), nullable=True)
    leave_13: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ext1: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ext2: Mapped[str | None] = mapped_column(String(255), nullable=True)
    business_01: Mapped[str | None] = mapped_column(String(255), nullable=True)
    business_02: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 源视图撤回后保留旧字段供追溯，业务计算必须排除该行。
    source_withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class DingtalkDepartmentRaw(Base):
    """钉钉通讯录原始部门树（listsub 同步而来，成员同步复用）。"""

    __tablename__ = "dingtalk_department_raw"

    id: Mapped[int] = mapped_column(primary_key=True)
    dept_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    parent_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    layer: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    face_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    root_dept_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class DingtalkEmployeeRaw(Base):
    """钉钉通讯录原始员工（user/list 同步而来，按 userid 去重）。"""

    __tablename__ = "dingtalk_employee_raw"

    id: Mapped[int] = mapped_column(primary_key=True)
    userid: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    job_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    title: Mapped[str | None] = mapped_column(String(128), nullable=True)
    mobile: Mapped[str | None] = mapped_column(String(32), nullable=True)
    unionid: Mapped[str | None] = mapped_column(String(128), nullable=True)
    hired_date: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    active: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    dept_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    dept_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    root_label: Mapped[str | None] = mapped_column(String(64), nullable=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class DingtalkSyncState(Base):
    """钉钉同步进度状态（后台任务进度，前端轮询）。"""

    __tablename__ = "dingtalk_sync_state"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), unique=True)
    status: Mapped[str] = mapped_column(String(16), default="idle")
    phase: Mapped[str | None] = mapped_column(String(32), nullable=True)
    current_root: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dept_total: Mapped[int] = mapped_column(Integer, default=0)
    dept_done: Mapped[int] = mapped_column(Integer, default=0)
    member_total_depts: Mapped[int] = mapped_column(Integer, default=0)
    member_done_depts: Mapped[int] = mapped_column(Integer, default=0)
    member_count: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # attendance_task_lease 行使用；数据库租约让多 worker/独立调度进程共享互斥状态。
    owner_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class DingtalkEmployeeSyncConfig(Base):
    """员工差异自动同步全局配置；规则 JSON 由服务层校验。"""

    __tablename__ = "dingtalk_employee_sync_config"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    rules_json: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
    updated_by: Mapped[str | None] = mapped_column(String(64), nullable=True)


class DingtalkEmployeeSyncWhitelist(Base):
    """不受任何员工差异同步影响的员工。"""

    __tablename__ = "dingtalk_employee_sync_whitelist"

    emp_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    created_by: Mapped[str] = mapped_column(String(64))


class DingtalkEmployeeSyncRun(Base):
    """手动/自动员工差异同步的后台运行状态与汇总。"""

    __tablename__ = "dingtalk_employee_sync_run"

    id: Mapped[int] = mapped_column(primary_key=True)
    trigger: Mapped[str] = mapped_column(String(16))  # manual | auto
    actor: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="running")
    phase: Mapped[str | None] = mapped_column(String(32), nullable=True)
    rule_json: Mapped[str] = mapped_column(Text, default="{}")
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RosterRootDept(Base):
    """钉钉通讯录同步范围：根部门（描述、钉钉部门 ID、归属地区）。

    HR 在「通讯录同步」页可编辑此列表（描述/部门 ID/地区）。空表时同步服务回退到
    DEFAULT_ROSTER_ROOT_DEPTS（见 dingtalk_employee_service）。按 sort,id 决定迭代顺序。
    """

    __tablename__ = "roster_root_dept"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(128), unique=True)  # 描述（展示名）
    dept_id: Mapped[int] = mapped_column(BigInteger)  # 钉钉部门 ID（根部门）
    region_code: Mapped[str | None] = mapped_column(ForeignKey("region.code"), nullable=True)
    sort: Mapped[int] = mapped_column(Integer, default=0)  # 迭代/展示顺序


class RosterTitleMap(Base):
    """钉钉职位 → 角色/常用职位 对照字典。HR 在「通讯录同步」页维护。

    转花名册时按 title 命中本表：写 employee.role（role.code）+ 员工「常用岗位」收藏
    （employee_favorite kind=post）。role=chief 时还按部门自动绑定工作点-值班长。
    role/post 允许为空（只映射其一）。
    """

    __tablename__ = "roster_title_map"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(128), unique=True)  # 钉钉职位（原始 title）
    role: Mapped[str | None] = mapped_column(String(16), nullable=True)  # → role.code（可空）
    post: Mapped[str | None] = mapped_column(String(64), nullable=True)  # 常用职位（岗位名，可空）
    sort: Mapped[int] = mapped_column(Integer, default=0)


class ReconResult(Base):
    __tablename__ = "recon_result"

    id: Mapped[int] = mapped_column(primary_key=True)
    emp_id: Mapped[str] = mapped_column(String(32))
    date: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(32))
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    # HR / 值班长 逐条确认（三态：通过/不通过/未确认，相互独立；自动一致默认两者通过）
    hr_status: Mapped[str] = mapped_column(String(48), default="未确认", server_default="未确认")
    hr_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    hr_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    chief_status: Mapped[str] = mapped_column(
        String(48), default="未确认", server_default="未确认"
    )
    chief_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    chief_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # HR 委托时固化目标工作点，避免后续考勤补录/撤销导致待办漂移到另一工作点。
    # 历史记录为空时，读取路径仍按当日考勤归属兜底。
    delegate_furnace_id: Mapped[int | None] = mapped_column(
        ForeignKey("furnace.id"), nullable=True
    )

    __table_args__ = (UniqueConstraint("emp_id", "date"),)


class ChiefConfirm(Base):
    __tablename__ = "chief_confirm"

    id: Mapped[int] = mapped_column(primary_key=True)
    furnace_id: Mapped[int] = mapped_column(ForeignKey("furnace.id"))
    natual_date: Mapped[str] = mapped_column(String(10))
    locked: Mapped[bool] = mapped_column(Boolean, default=False)
    confirmed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (UniqueConstraint("furnace_id", "natual_date"),)


class MonthlyAbsenceOverride(Base):
    __tablename__ = "monthly_absence_override"
    month: Mapped[str] = mapped_column(String(7), primary_key=True)
    emp_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    days: Mapped[Decimal | None] = mapped_column(Numeric(4, 2), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    updated_by: Mapped[str] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class MonthlyExportLock(Base):
    """工作点级月度导出锁：锁定后禁止改变该工作点当月导出结果的写操作。"""

    __tablename__ = "monthly_export_lock"

    id: Mapped[int] = mapped_column(primary_key=True)
    month: Mapped[str] = mapped_column(String(7))
    furnace_id: Mapped[int] = mapped_column(ForeignKey("furnace.id"))
    locked: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    locked_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    unlocked_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    unlocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    __table_args__ = (UniqueConstraint("month", "furnace_id"),)


class ShiftWindow(Base):
    __tablename__ = "shift_window"

    id: Mapped[int] = mapped_column(primary_key=True)
    region: Mapped[str] = mapped_column(String(64), unique=True)
    region_code: Mapped[str | None] = mapped_column(ForeignKey("region.code"), nullable=True)
    early_from: Mapped[str] = mapped_column(String(32))
    early_to: Mapped[str] = mapped_column(String(32))
    mid_from: Mapped[str] = mapped_column(String(32))
    mid_to: Mapped[str] = mapped_column(String(32))
    late_from: Mapped[str] = mapped_column(String(32))
    late_to: Mapped[str] = mapped_column(String(32))


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Redis Stream 消费至少一次；event_id 让 worker 在“数据库已提交、Redis 尚未确认”
    # 的崩溃窗口重放时保持幂等。历史/业务审计不经过队列，允许为空。
    event_id: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    actor: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 用带时区的当前时刻（aware UTC）：naive utcnow 入 timestamptz 会被会话时区(+08)误localize，
    # 导致审计时间整体偏移 8 小时。索引 ts 便于按时间范围/倒序查询。
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)


class AppSetting(Base):
    """Runtime-editable key/value settings (e.g. proxy_signin_monthly_limit)."""

    __tablename__ = "app_setting"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(256))


class ProxySigninGrant(Base):
    """Temporary per-worker/month extra 代签 allowance granted by hr/stat."""

    __tablename__ = "proxy_signin_grant"

    id: Mapped[int] = mapped_column(primary_key=True)
    emp_id: Mapped[str] = mapped_column(String(32))
    month: Mapped[str] = mapped_column(String(7))  # YYYY-MM
    extra: Mapped[int] = mapped_column(Integer, default=0)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor: Mapped[str] = mapped_column(String(64))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class EscalationRecord(Base):
    __tablename__ = "escalation_record"

    id: Mapped[int] = mapped_column(primary_key=True)
    furnace_id: Mapped[int] = mapped_column(ForeignKey("furnace.id"))
    natual_date: Mapped[str] = mapped_column(String(10))
    level: Mapped[int] = mapped_column(Integer, default=1)
    targets: Mapped[str] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(32), default="open")
    first_escalated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (UniqueConstraint("furnace_id", "natual_date", "level"),)


class NotificationRecord(Base):
    __tablename__ = "notification_record"

    id: Mapped[int] = mapped_column(primary_key=True)
    channel: Mapped[str] = mapped_column(String(32))
    target: Mapped[str] = mapped_column(String(256))
    payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    # 班次推送等：status=sent/skipped；kind=shift_push（历史记录留空）
    status: Mapped[str] = mapped_column(String(16), default="", server_default="")
    kind: Mapped[str] = mapped_column(String(32), default="", server_default="")


class SyncExecutionLog(Base):
    """每次同步执行的独立历史；进度状态表被下一次任务覆盖时仍保留。"""
    __tablename__ = "sync_execution_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    trigger: Mapped[str] = mapped_column(String(16), index=True)
    actor: Mapped[str] = mapped_column(String(64))
    region: Mapped[str | None] = mapped_column(String(128), index=True)
    date_from: Mapped[str | None] = mapped_column(String(10))
    date_to: Mapped[str | None] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(16), default="running", index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    counts_json: Mapped[str] = mapped_column(Text, default="{}")
    message: Mapped[str | None] = mapped_column(Text)
