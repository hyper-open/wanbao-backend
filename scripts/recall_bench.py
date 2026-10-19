"""召回回归压测：真实措辞查询 × 混合召回。

用法（在容器内）：
    docker exec -i wanbao python - < scripts/recall_bench.py

- CASES：真实用户措辞 → 期望工具（命中 = 期望工具出现在召回候选里）
- 每次加新工具/改描述后跑一遍，防召回退化；漏召回先补标签/描述再考虑改代码
"""
import time

from app.core.db import SessionLocal
from app.models import Tool
from app.services.agent.recall import rank_tools

CASES = [
    ("把这篇英文文章翻译成中文", "translate"),
    ("我要跟日本客户开会，帮我翻译成日语", "translate"),
    ("帮我写本周工作总结", "weekly_report"),
    ("年底了要写述职报告", "annual_review"),
    ("这段文案太生硬，帮我改得自然点", "copy_rewrite"),
    ("给客户写一封商务邮件", "email_write"),
    ("把会议内容整理成纪要", "meeting_minutes"),
    ("这篇文章太长，帮我提炼要点", "summarize"),
    ("帮我把这几句话扩写成一段话", "expand"),
    ("写一篇小红书笔记种草火锅", "xhs_copy"),
    ("帮我优化简历", "resume_optimize"),
    ("PPT 太乱了帮我重排", "pptx_optimize"),
    ("想几个吸引人的标题", "title_gen"),
    ("写一篇公众号文章介绍我们产品", "wechat_article"),
    ("发个朋友圈文案", "moments_copy"),
    ("给我们的保温杯写卖点文案", "product_desc"),
    ("准备一场直播的话术", "live_script"),
    ("帮我列一个文章大纲", "outline"),
    ("婚礼上我要致辞", "wedding_speech"),
    ("中秋节给客户发祝福", "greeting"),
    ("给我的奶茶店起个名字", "name_ideas"),
    ("Excel 里怎么统计重复项", "excel_formula"),
    ("起草一份放假通知", "official_notice"),
    ("帮我们产品做个 SWOT 分析", "swot"),
    ("写一个前端工程师的招聘 JD", "job_desc"),
    ("帮我改这篇作文", "essay_polish"),
    ("写一篇《活着》读后感", "book_report"),
    ("给孩子编一个关于分享的睡前故事", "kids_story"),
    ("帮我出 10 道练习题", "quiz_gen"),
    ("推荐几本管理学书籍", "book_recommend"),
    ("用大白话解释什么是区块链", "concept_explain"),
    ("我想三个月学会 Python，帮我定计划", "study_plan"),
    ("把这些知识点做成记忆卡片", "flashcard_gen"),
    ("帮我分析这道题为什么做错", "mistake_analysis"),
    ("写一篇技术博客分享我们的架构升级", "tech_blog"),
    ("帮我写个爬虫用的正则表达式", "regex_gen"),
    ("这段 SQL 查询怎么写", "sql_write"),
    ("提交代码的 message 怎么写", "git_message"),
    ("帮这段代码写单元测试", "unit_test"),
    ("解释一下这段代码干了什么", "code_explain"),
    ("我们要做一个电商小程序，技术选型怎么定", "tech_choice"),
    ("服务器老是报错，帮我分析日志", "log_analysis"),
    ("设计一套 App 的配色方案", "color_scheme"),
    ("给我的咖啡品牌写个 logo 需求", "logo_brief"),
    ("活动海报文案怎么写", "poster_copy"),
    ("界面上的报错提示文案怎么写", "ui_copy"),
    ("用 Midjourney 画一只猫的提示词", "ai_prompt"),
    ("帮我把需求改写成 AI 提示词", "prompt_optimize"),
    ("这张照片配什么文案好", "photo_caption"),
    ("拍个 vlog 的脚本", "vlog_script"),
    ("写一条抖音短视频脚本", "douyin_script"),
    ("剪视频有什么思路", "video_edit"),
    ("手机拍美食有什么技巧", "phone_photo"),
    ("新品上市的传播计划", "product_launch"),
    ("怎么提升店铺评分", "delivery_ops"),
    ("开一家小面馆要准备什么", "restaurant_plan"),
    ("后厨怎么标准化", "kitchen_sop"),
    ("跨境电商 listing 怎么本地化", "listing_localize"),
    ("亚马逊客户投诉怎么回复", "overseas_cs"),
    ("发票抬头开错了怎么办", "invoice_manage"),
    ("个税专项扣除有哪些", "personal_tax"),
    ("品牌的语调指南怎么做", "brand_voice"),
    ("门店周年庆促销活动策划", "retail_promo"),
    ("一对一辅导怎么备课", "tutoring_plan"),
    ("给家长写学生学习报告", "student_report"),
    ("教我爸妈用微信视频通话", "elder_phone"),
    ("孩子沉迷手机怎么办", "kids_screen"),
    ("辅导作业总是发火怎么办", "kids_homework"),
    ("家里太乱怎么断舍离", "room_clean"),
    ("周末带家人去哪玩", "weekend_plan"),
    ("第一次露营要带什么", "camping"),
    ("城市漫步路线怎么规划", "citywalk_route"),
    ("自驾去西藏要注意什么", "road_trip"),
    ("办签证需要哪些材料", "visa_checklist"),
    ("带我家猫一起去旅行", "pet_travel"),
    ("酒店怎么挑选", "hotel_choice"),
    ("想学吉他零基础", "music_instrument"),
    ("围棋入门怎么学", "chess_go"),
    ("跑步从零开始怎么练", "running_plan"),
    ("帮我对比两个方案哪个更合适", "decision_matrix"),
    ("新团队怎么制定协作章程", "team_charter"),
    ("系统技术债太多了怎么办", "tech_debt"),
    ("团队代码规范怎么制定", "code_style"),
    ("产品想加 AI 功能怎么入手", "ai_integration"),
    ("系统监控告警怎么做", "observability"),
    ("数据库备份策略怎么设计", "db_backup"),
    ("服务器出过几次故障怎么写复盘", "incident_postmortem"),
    ("云服务器费用太高怎么省", "infra_cost"),
    ("数据太脏了怎么清洗", "data_clean"),
    ("给老板做一个数据看板", "dashboard_design"),
    ("怎么夸奖别人显得真诚", "smart_reply"),
    ("朋友失恋了怎么安慰", "friend_support"),
    ("和同事闹矛盾了怎么化解", "conflict_resolution"),
    ("相亲第一次见面怎么自我介绍", "dating_profile"),
    ("给十年后的自己写封信", "letter_to_future"),
    ("压力太大怎么调节", "stress_relief"),
    ("养一只柯基需要准备什么", "pet_guide"),
    ("多肉植物怎么养", "plant_guide"),
    ("周末想看点电影", "movie_recommend"),
    ("家庭聚餐做什么菜", "recipe_idea"),  # 聚餐菜谱语义更贴近菜谱推荐而非一周食谱
    ("想学做面包烘焙", "bake_recipe"),
    ("茶叶怎么选购", "tea_guide"),
    ("收藏球鞋有什么要注意的", "collectibles"),
    ("咖啡豆怎么选", "coffee_guide"),
    ("要不要辞职去创业，帮我权衡", "decision_matrix"),
    ("邀请函怎么写", "invitation_write"),
    ("怎么写感谢信", "thank_letter"),
    ("请假条格式", "leave_note"),
    ("帮我写一首藏头诗", "acrostic_poem"),
    ("写一段脱口秀段子", "standup_joke"),
    ("出几个谜语给大家猜", "riddle_write"),
    ("绕口令练习", "tongue_twister"),
    ("聚会玩什么游戏", "game_ideas"),
    ("二手相机挂闲鱼怎么定价", "second_hand"),
    ("汽车多久保养一次", "car_maintenance"),
    ("骑自行车上班安全吗", "bike_commute"),
    ("怎么教爷爷奶奶用手机", "elder_phone"),
    ("志愿服务去哪里报名", "volunteer"),
    ("营销短信怎么写", "push_copy"),
    ("播客第一期做什么内容", "podcast_script"),
    ("newsletter 怎么写", "newsletter"),
    ("白皮书框架怎么搭", "white_paper"),
    ("落地页转化率低怎么办", "landing_page"),
    ("小红书探店文案", "food_review"),
    ("留学申请的个人陈述怎么写", "application_essay"),
    ("老师怎么给家长写沟通消息", "parent_message"),
    ("作业评语怎么写", "homework_comment"),
    ("少儿编程从几岁开始", "coding_kids"),
    ("教案怎么写", "lesson_plan"),
    ("课堂互动游戏设计", "class_activity"),
    ("帮学生写推荐信", "recommendation_letter"),
    ("简历投出去没回音，帮我写求职信", "cover_letter"),
    ("面试被问离职原因怎么答", "mock_interview"),
    ("租房合同要注意什么", "rent_guide"),
    ("搬家怎么打包", "moving_checklist"),
    ("办公室久坐腰疼怎么缓解", "stretching_flow"),
    ("保险怎么配置比较合理", "insurance_basic"),
    ("想学调酒", "cocktail_home"),
    ("读书笔记怎么做", "reading_notes"),
    ("快速阅读怎么训练", "speed_reading"),
    ("帮我写一份 SOP", "sop_write"),
    ("产品说明书怎么写", "user_manual"),
    ("会议议程怎么安排", "meeting_agenda"),
    ("项目计划书怎么写", "project_plan"),
    ("出差报告模板", "trip_report"),
    ("工作交接文档怎么写", "handover_doc"),
    ("写一篇关于思乡的诗歌", "poem_write"),
    ("把这段话改写成鲁迅的文风", "writing_style"),
    ("我想找个新爱好", "hobby_finder"),
    ("怎么让孩子少玩手机", "kids_screen"),
    ("办一场生日惊喜", "birthday_plan"),
    ("婚礼筹备清单", "wedding_prep"),
    ("帮我设计一条骑行通勤路线", "bike_commute"),
    ("写作没灵感帮我出点子", "writing_prompt"),
    # —— BATCH13 doc/data 深度补强 ——
    ("帮我看看这份合同有没有坑", "contract_review"),
    ("审一下这份采购协议的风险点", "contract_review"),
    ("帮我润色这段论文，要更学术一点", "thesis_polish"),
    ("工作报告写得太水了，帮我改改", "report_polish"),
    ("这篇文档太长，帮我总结一下核心内容", "doc_summary"),
    ("论文格式不对，帮我按学校规范调整", "doc_format"),
    ("我们准备投标，帮我拆解一下招标文件的要求", "tender_review"),
    ("给新员工写一份系统操作手册", "manual_write"),
    ("要做一份用户调研报告，帮我搭个框架", "report_outline"),
    ("这段 SQL 帮我看看为什么慢", "sql_explain"),
    ("帮我设计一个记账用的 Excel 表", "excel_template"),
    ("根据这些数据帮我写一份月度分析报告", "data_report"),
    ("分析下我们下单转化漏斗哪一步流失最多", "funnel_analysis"),
    ("活跃用户这个指标到底怎么算，帮我定义清楚", "metric_definition"),
    ("看看我们新用户的留存数据表现怎么样", "cohort_analysis"),
    ("这次 A/B 实验结果出来了，帮我看看要不要上线", "experiment_review"),
    ("问卷回收了几百份，帮我整理分析下", "survey_summary"),
]

db = SessionLocal()
tools = db.query(Tool).filter(Tool.enabled == True).all()  # noqa: E712

hit = miss = 0
lat = []
misses = []
t_all = time.time()
for text, exp in CASES:
    t0 = time.time()
    top = [t.id for t, _ in rank_tools(db, tools, text)]
    dt = time.time() - t0
    lat.append(dt)
    if exp in top:
        hit += 1
    else:
        miss += 1
        misses.append((text, exp, top[:8]))
total = time.time() - t_all

lat.sort()
p50 = lat[len(lat) // 2]
p95 = lat[int(len(lat) * 0.95)]
print(f"用例 {len(CASES)} | 命中 {hit} | 漏召回 {miss} | 命中率 {hit/len(CASES)*100:.1f}%")
print(f"延迟：p50 {p50*1000:.0f}ms | p95 {p95*1000:.0f}ms | max {max(lat)*1000:.0f}ms | 总耗时 {total:.1f}s")
if misses:
    print("--- 漏召回明细 ---")
    for text, exp, top in misses:
        print(f"  「{text}」期望 {exp} | 实际候选 {top}")
