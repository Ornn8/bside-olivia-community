"""English wording for the fixed texts of her daily life, as the World page shows them.

The life store, the prompts and the reply context keep these texts in Chinese and nothing here changes that. When the
interface is English, the API that feeds the page asks this module to put English wording on the copy it sends: the
activity names, the places, the rest rhythm, the timetable, the stock episodes and the sentences the program composes
from them. Wording that a model wrote (what she practices, the shops she visits, her own words) has no entry and is
passed through as it is; a sentence that mixes the two keeps the model's words inside the English frame.

Chinese mode returns the very object it was given, so nothing changes there.
"""

from __future__ import annotations

import re
from typing import Any

from runtime import ui_language

# --- places -------------------------------------------------------------------------------------------------------
# name -> (label on its own, in a sentence, bare noun phrase after "to")
PLACES: dict[str, tuple[str, str, str]] = {
    "家里": ("Home", "At home", "home"),
    "住处": ("Her place", "At her place", "her place"),
    "学校": ("School", "At school", "school"),
    "住处附近": ("Near home", "Near home", "somewhere near home"),
    "店里": ("In a shop", "In a shop", "a shop"),
    "浴室": ("Bathroom", "In the bathroom", "the bathroom"),
    "卧室": ("Bedroom", "In her bedroom", "her bedroom"),
    "家里音乐房": ("Music room at home", "In the music room at home", "the music room at home"),
    "学校琴房": ("School practice room", "In a practice room at school", "a practice room at school"),
    "学校教室": ("School classroom", "In a classroom at school", "a classroom at school"),
}
# Rooms and shops the day-plan writer is told to use (exact names only); a room at home may carry "家里".
PLAN_PLACES: dict[str, tuple[str, str, str]] = {
    "厨房": ("Kitchen", "In the kitchen", "the kitchen"),
    "音乐房": ("Music room", "In the music room", "the music room"),
    "客厅": ("Living room", "In the living room", "the living room"),
    "阳台": ("Balcony", "On the balcony", "the balcony"),
    "书房": ("Study", "In the study", "the study"),
    "餐桌": ("Dining table", "At the dining table", "the dining table"),
    "琴房": ("Practice room", "In a practice room", "a practice room"),
    "图书馆": ("Library", "In the library", "the library"),
    "食堂": ("Canteen", "In the canteen", "the canteen"),
    "商场服装店": ("Clothing store in a mall", "At a clothing store in a mall", "a clothing store in a mall"),
    "超市": ("Supermarket", "At the supermarket", "the supermarket"),
    "文具店": ("Stationery shop", "At the stationery shop", "the stationery shop"),
    "花店": ("Flower shop", "At the flower shop", "the flower shop"),
    "面包店": ("Bakery", "At the bakery", "the bakery"),
    "展馆": ("Exhibition hall", "At the exhibition hall", "the exhibition hall"),
    "公园": ("Park", "In the park", "the park"),
    "校门口的文具店": ("Stationery shop by the school gate", "At the stationery shop by the school gate",
                       "the stationery shop by the school gate"),
}

# --- activities ---------------------------------------------------------------------------------------------------
# label -> (title, phrase used inside a sentence)
ACTIVITIES: dict[str, tuple[str, str]] = {
    "休息": ("Resting", "resting"),
    "醒来": ("Waking up", "waking up"),
    "开始洗澡": ("Starting her bath", "starting her bath"),
    "刚洗完澡": ("Just finished her bath", "just finished her bath"),
    "练琴": ("Piano practice", "practicing the piano"),
    "阅读": ("Reading", "reading"),
    "整理家务": ("Housework", "doing housework"),
    "散步": ("Out for a walk", "out for a walk"),
    "办日常杂事": ("Running errands", "running errands"),
    "创作": ("Creative work", "doing creative work"),
    "买日常用品": ("Shopping for everyday things", "shopping for everyday things"),
}

# What she can be asked to focus on when no day plan offers anything else.
FOCUSES: dict[str, str] = {
    "乐句衔接": "phrase transitions",
    "节奏与触键": "rhythm and touch",
    "视奏片段": "sight-reading passages",
    "困难小节": "difficult bars",
    "专业课笔记": "notes from her major classes",
    "选定章节": "a chosen chapter",
    "借阅的书": "a library book",
    "桌面与书架": "desk and bookshelf",
    "衣物": "clothes",
    "窗边的植物": "plants by the window",
    "附近的街道": "the streets nearby",
    "校园步道": "campus paths",
    "生活用品": "everyday supplies",
    "日常采购": "everyday shopping",
    "日常需要": "everyday needs",
    "短旋律动机": "a short melodic motif",
    "和声草稿": "a harmony sketch",
    "练习录音": "a practice recording",
    "课堂笔记": "class notes",
    # The examples the day-plan writer is given; it often repeats them as they are.
    "和声学作业第三题": "question 3 of the harmony assignment",
    "乐团分谱第二乐章": "the second movement of her orchestra part",
    "图书馆借的传记": "a biography borrowed from the library",
    "阳台上的衣物": "clothes on the balcony",
    "校门口的文具店": "the stationery shop by the school gate",
}

COURSES: dict[str, str] = {
    "和声": "Harmony",
    "钢琴专业课": "Piano major class",
    "大学外语": "College foreign language",
    "心理学": "Psychology",
    "体育": "Physical education",
    "音乐史": "Music history",
    "视唱练耳": "Solfège and ear training",
    "室内乐排练": "Chamber music rehearsal",
    "公共课": "General course",
    "演奏交流": "Performance exchange",
}

# --- meals --------------------------------------------------------------------------------------------------------
MEAL_SLOTS: dict[str, str] = {
    "早饭": "breakfast", "早餐": "breakfast", "午饭": "lunch", "午餐": "lunch",
    "晚饭": "dinner", "晚餐": "dinner", "加餐": "a snack",
}
MEAL_VERBS: dict[str, tuple[str, str]] = {
    "准备吃": ("About to have {meal}", "about to have {meal}"),
    "正在吃": ("Having {meal}", "having {meal}"),
    "刚吃完": ("Just finished {meal}", "just finished {meal}"),
    "没吃": ("Skipped {meal}", "skipped {meal}"),
}
MEAL_STAGES: dict[str, tuple[str, str]] = {
    "已下单，等待外卖": ("Waiting for the {meal} delivery", "waiting for the {meal} delivery"),
    "正在准备餐食": ("Preparing {meal}", "preparing {meal}"),
    "准备出门就餐": ("Heading out for {meal}", "heading out for {meal}"),
    "准备买饭带回": ("Going to pick up {meal}", "going to pick up {meal}"),
    "正在吃": ("Having {meal}", "having {meal}"),
}
# The same stages without a meal name (the note of a meal starts with them).
MEAL_STAGE_ALONE: dict[str, tuple[str, str]] = {
    "已下单，等待外卖": ("Delivery ordered, waiting", "delivery ordered, waiting"),
    "正在准备餐食": ("Preparing the meal", "preparing the meal"),
    "准备出门就餐": ("Heading out to eat", "heading out to eat"),
    "准备买饭带回": ("Going to pick up a meal", "going to pick up a meal"),
    "正在吃": ("Eating", "eating"),
}
FOODS: dict[str, str] = {
    "豆浆和包子": "soy milk and steamed buns", "鸡蛋面": "egg noodles", "粥和小菜": "congee with side dishes",
    "牛奶和面包": "milk and bread", "米饭和番茄炒蛋": "rice with tomato and scrambled eggs",
    "青菜肉丝面": "noodles with greens and shredded pork", "鸡肉盖饭": "chicken rice bowl",
    "蔬菜饺子": "vegetable dumplings", "鱼肉和米饭": "fish with rice",
    "米饭和炒时蔬": "rice with stir-fried seasonal vegetables", "菌菇面": "mushroom noodles",
    "馄饨": "wontons", "鸡肉和米饭": "chicken with rice", "杂粮粥": "multigrain congee",
    "水果": "fruit", "酸奶": "yogurt", "面包": "bread", "坚果": "nuts",
    # The examples the day-plan writer is given.
    "二食堂的番茄牛腩饭": "tomato beef rice from Canteen 2", "楼下的煎饼果子": "jianbing from downstairs",
    "外卖的酸辣粉": "hot and sour noodles from delivery", "番茄牛腩饭": "tomato beef rice",
}

# --- rhythm, wellbeing, timetable -----------------------------------------------------------------------------------
RHYTHM: dict[str, str] = {
    "林离洗澡中": "Taking a bath",
    "睡觉的时段：这时收到对方的消息，就是把睡着的她吵醒了": "Her sleeping time: a message now wakes her up",
    "夜里被吵醒后还醒着，在和对方聊天": "Woken in the night and still awake, chatting with you",
    "早餐时间": "Breakfast time",
    "午饭时间": "Lunch time",
    "晚饭时间": "Dinner time",
    "准备收工休息": "Winding down for the day",
    "留给练习和创作的时间": "Time set aside for practice and creating",
    "自己的闲暇时间": "Her own free time",
    "夜间睡眠中": "Sleeping for the night",
    "补觉中": "Taking a nap",
    "休息不足，今天减少安排，把休息放在前面。": "She is short on rest, so today she is doing less and putting rest first.",
    "最近休息受到影响，放慢一点，留时间补觉。": "Her rest has been disturbed lately, so she is slowing down and leaving time to catch up on sleep.",
    "按自己的节奏生活。": "Living at her own pace.",
    "这段休息后精力有所恢复，可以按意愿接回轻活动。": "Her energy has recovered after this rest, and she can ease back into light activities if she likes.",
    "这段休息后缓过来一些，仍适合放慢节奏。": "She feels somewhat better after this rest, but a slower pace still suits her.",
    "连续休息不足，身体不太舒服，减少练习；若休息后仍不适，安排门诊咨询。":
        "She hasn't been resting enough and doesn't feel well, so she is practicing less. If she still feels unwell after resting, she will arrange to see a doctor.",
    "连续休息不足，身体不太舒服，今天先减少活动、好好休息。":
        "She hasn't been resting enough and doesn't feel well, so today she is doing less and resting properly.",
    "正在慢慢恢复，先不把日程排满。": "She is slowly recovering and isn't filling up her schedule yet.",
    "没有持续身体不适的记录。": "No record of ongoing discomfort.",
    "这段休息后精力有所恢复。": "Her energy has recovered after this rest.",
    "恢复后又有新的消耗，放慢节奏安排生活。": "She has been drained again after recovering, so she is taking things slowly.",
}

# --- stock episodes: motives, obstacles, responses, results, meanings, next steps ---------------------------------
EPISODES: dict[str, str] = {
    # motives
    "现在开始自己的这次洗澡": "Starting her bath now",
    "为自己买本次需要的日常用品": "Buying the everyday things she needs this time",
    "完成自己这次洗澡并准备擦干头发": "Finishing her bath and getting ready to dry her hair",
    "跟进当前正在进行的课程": "Following the class that is going on",
    "继续理解手头正在读的内容": "Working through what she is reading",
    "推进自己当前的小段创作": "Moving her current piece of creative work forward",
    "给自己留一点活动和观察的空间": "Giving herself some room to move and look around",
    "处理自己当前需要办的一件小事": "Taking care of a small thing she needs to do",
    "整理自己使用的生活空间": "Tidying the space she lives in",
    "按今天自己的安排做这件事": "Doing this as part of her plan for today",
    "继续当前自己已经选定的这一小步": "Carrying on with the small step she has chosen",
    "想把当前选定的小片段练得更稳": "Wanting to make the passage she picked steadier",
    "继续处理当前练习中的未完成部分": "Continuing with the unfinished part of her practice",
    "想恢复精力": "Wanting to get her energy back",
    "想暂时留出不处理任务的时间": "Wanting some time away from tasks",
    "给自己留出这一餐的时间": "Making time for this meal",
    "照顾当下的进食需要": "Looking after her need to eat",
    # bath
    "准备开始清洁": "Getting ready to wash",
    "进入浴室，开始本次洗澡": "Went into the bathroom and started her bath",
    "已经开始这次洗澡，还没有洗完。": "She has started her bath and hasn't finished yet.",
    "洗澡结束后头发还湿着": "Her hair was still wet after the bath",
    "关好水并裹好浴巾，准备擦头发": "Turned off the water, wrapped up in a towel and got ready to dry her hair",
    "刚洗完这次澡，已经裹好浴巾，头发还没完全干。": "She has just finished her bath, is wrapped in a towel, and her hair isn't quite dry yet.",
    "发梢还在滴水": "Her hair was still dripping",
    "裹好浴巾后先用毛巾擦发梢": "Wrapped up in a towel and dried the ends of her hair first",
    "这次洗澡已经完成，裹好浴巾后正在擦头发。": "Her bath is done; she is wrapped in a towel and drying her hair.",
    # shopping
    "日常用品需要补充": "Everyday supplies needed restocking",
    "选好本次需要的用品，结账后装进袋子": "Picked out what she needed, paid and put it in a bag",
    "这次需要的日常用品已经买好，装进了袋子。": "She has bought the everyday things she needed and put them in a bag.",
    "选项有点多": "There were a lot of options",
    "比较后选好当前需要的用品，结账并收好": "Compared them, chose what she needed, paid and packed it away",
    "这次购物已经完成，买好的东西收进了袋子。": "The shopping is done, and what she bought is in the bag.",
    "当前没有合适的选项": "Nothing suitable at the moment",
    "这次先不买，结束挑选": "Decided not to buy anything this time and stopped browsing",
    "这次没有买到合适的用品，之后再找。": "She didn't find anything suitable this time and will look again later.",
    # class
    "概念还没有理清": "The concepts weren't clear yet",
    "对照自己的笔记重新整理": "Went back over her notes to sort them out",
    "当前这一处内容理清了一点，课程仍在进行。": "She has this part a bit clearer; the class is still going.",
    "注意力有些分散": "Her attention was drifting",
    "把注意力收回当前内容，记下疑问": "Brought her attention back to the lesson and wrote down her questions",
    "记下了还未弄懂的问题，尚未解决，课程仍在进行。":
        "She has written down what she still doesn't understand; it isn't settled, and the class is still going.",
    "有一部分课堂内容还没理解": "Part of the lesson wasn't clear yet",
    "先记下疑问，继续跟进课程内容": "Wrote down her questions and kept following the lesson",
    "课堂上有一部分内容还没理解，已记下疑问，留待继续梳理。":
        "She didn't understand part of the lesson, hasn'ted her questions and will sort them out later.",
    # reading
    "一段意思不容易把握": "One passage was hard to grasp",
    "回读前后文并做简短笔记": "Re-read the text around it and made short notes",
    "理清了部分意思，还有一处疑问。": "She has made sense of part of it, with one question left.",
    "阅读节奏断断续续": "Her reading kept stopping and starting",
    "缩小到当前这一小段": "Narrowed down to the short section in front of her",
    "完成了这一小段阅读和整理。": "She has finished reading and summarizing this short section.",
    "注意力维持不住": "She couldn't keep her focus",
    "标记读到的位置，暂时合上": "Marked her place and closed the book for now",
    "这次阅读暂停，未读部分留待之后。": "Reading is paused; the rest is left for later.",
    # creative
    "想法还不够清楚": "The idea wasn't clear enough yet",
    "先做一个粗略版本再比较": "Made a rough version first to compare",
    "有了一个初稿，仍需修改。": "She has a first draft that still needs work.",
    "细节不够顺手": "The details weren't coming together",
    "删减多余部分，完成一个小版本": "Cut what wasn't needed and finished a small version",
    "完成了本次选定的小版本。": "She finished the small version she had picked.",
    "几种尝试都不满意": "None of her attempts satisfied her",
    "保留草稿，暂时停下来": "Kept the draft and stopped for now",
    "这次没有得到满意的版本，问题仍保留。": "She didn't get a version she was happy with this time, and the problem remains.",
    # walk
    "步调有点急": "Her pace was a bit hurried",
    "放慢速度，留意脚下和周围": "Slowed down and paid attention to her steps and surroundings",
    "已经走了一小段，还在慢慢走。": "She has walked a little way and is still strolling.",
    "脑中仍挂着待办": "Her to-do list was still on her mind",
    "暂时把注意力放到行走上": "Put her attention on walking for now",
    "结束了这一小段散步。": "She has finished this short walk.",
    "不想继续走太久": "She didn't want to walk for long",
    "缩短这次散步": "Cut the walk short",
    "这次散步提前停下。": "The walk ended early.",
    # errand
    "所需步骤还没理清": "The steps she needed weren't clear yet",
    "先检查自己已有的信息与准备": "Checked the information and preparation she already had",
    "完成了准备检查，事情尚未办完。": "She has finished checking her preparation, but the errand isn't done.",
    "随身所需物品还没整理好": "What she needed to carry wasn't sorted yet",
    "检查并整理这次自己需要带的东西": "Checked and sorted what she needs to take",
    "整理好了这次出门需要带的东西。": "She has sorted out what she needs to take with her.",
    "准备还有遗漏": "Her preparation still had gaps",
    "列出自己还需要补齐的部分": "Listed what she still needs to fill in",
    "这件事暂时搁置，仍有准备未完成。": "This is on hold for now; some preparation is still unfinished.",
    "这一轮未能推进": "She couldn't make progress this round",
    "记录目前卡住的步骤": "Noted the step she is stuck on",
    "本次未办成，已记下卡住的步骤。": "It didn't get done this time; she hasn'ted the step she is stuck on.",
    # housework
    "东西放得有些散": "Things were a bit scattered",
    "先只整理眼前的一小块": "Started with just the small area in front of her",
    "整理好了一部分，其余还没有动。": "Part of it is tidied; the rest is untouched.",
    "容易一边整理一边分心": "It was easy to get distracted while tidying",
    "把范围限定到当前的小区域": "Limited herself to the small area she was in",
    "完成了眼前这一小块的整理。": "She has finished tidying this small area.",
    "精力不足以继续": "She didn't have the energy to carry on",
    "收住当前动作，留下待整理部分": "Stopped what she was doing and left the rest for later",
    "家务暂时停下，还留有未完成的部分。": "The housework is paused, with some left to do.",
    # practice and the plain way to stop
    "注意力难以维持": "It was hard to keep her focus",
    "结束这一轮，给自己留出间隔": "Ended this round to give herself a break",
    "这次提前停下，之后再接着做。": "She stopped early this time and will pick it up again later.",
    "衔接还不够顺": "The transitions weren't smooth enough yet",
    "放慢速度，单独重复衔接处": "Slowed down and repeated the transition on its own",
    "这一处比开始时顺了一些，仍需再练。": "This spot is smoother than when she started, but it still needs practice.",
    "节奏容易赶": "She tended to rush the rhythm",
    "先分拍确认，再连起来练": "Checked the beats one by one, then played it through",
    "本次选定的小片段完成了一遍稳定的连贯练习。": "She played the passage she picked through once, steadily and smoothly.",
    "同一处反复失误": "She kept slipping in the same place",
    "缩小练习范围并尝试放慢": "Narrowed the practice range and tried slowing down",
    "这次仍未处理好，先停下来，保留这个问题。": "She still didn't get it right this time; she stopped for now and the problem remains.",
    "本次练习提前停下，目标还没有完成。": "Practice stopped early this time, and she hasn't reached her goal yet.",
    # rest and sleep
    "脑子还挂着待办": "Her mind was still on her to-do list",
    "先把待办放下，安静坐一会儿": "Put the to-do list aside and sat quietly for a while",
    "这一小段休息结束后，感觉节奏缓下来。": "After this short rest, she feels her pace has eased.",
    "仍惦记未完成的事": "She was still thinking about unfinished things",
    "尝试放松，但注意力又回到待办": "Tried to relax, but her attention drifted back to her tasks",
    "只放松了一点，还没完全缓过来。": "She only relaxed a little and hasn't quite settled.",
    "精力仍不足": "Her energy was still low",
    "减少手头活动，继续安静休息": "Cut back on what she was doing and kept resting quietly",
    "仍需要继续休息，没有开始新的任务。": "She still needs more rest and hasn't started anything new.",
    "需要从之前的消耗中缓过来": "She needed to recover from earlier exertion",
    "放下手头事务，安静休息后重新感受精力": "Set her tasks aside, rested quietly, then checked how her energy felt",
    "这段休息后精力有所恢复，可以按自己的意愿接回轻活动。":
        "Her energy has recovered after this rest, and she can ease back into light activities as she likes.",
    "想补一段觉来恢复精力": "She wanted a nap to get her energy back",
    "放下手头事务，开始补觉并留出明确的休息时段": "Set her tasks aside and started a nap with a set rest period",
    "准备结束今天的活动": "Getting ready to end the day's activities",
    "放下手头事务，开始今晚这一段睡眠": "Set her tasks aside and started tonight's sleep",
    "已经开始今晚的睡眠，计划醒来后再判断精力和接下来的安排。":
        "She has started sleeping for the night and will see how her energy is and what comes next once she wakes.",
    "昨夜已经开始的睡眠需要续接": "The sleep she began last night needed to be picked up",
    "结束这一夜的睡眠，睁开眼睛慢慢醒来": "Ended the night's sleep and slowly opened her eyes",
    "这一夜的睡眠已经结束，刚醒来，还在慢慢缓神。": "The night's sleep is over; she has just woken up and is still coming round.",
    "夜间睡眠期间又开始通信": "Messages started again during her night's sleep",
    "之前的夜间睡眠尚未记录结束，已经错过晨间醒来时段":
        "The earlier night's sleep hadn't been recorded as ended, and the morning waking time has passed",
    "明确结束之前这段睡眠，重新安排此刻的休息与活动": "Clearly ended the earlier sleep and rearranged her rest and activities for now",
    "之前这段睡眠现已结束，当前已经醒着；没有确认睡眠充分或精力恢复，之后再安排休息。":
        "The earlier sleep has ended and she is awake now; it isn't confirmed that she slept enough or recovered, so she will arrange more rest later.",
    "之前的补觉需要续接": "The nap she had started needed to be picked up",
    "结束已经开始的补觉，重新感受醒来后的精力": "Ended the nap she had started and checked how her energy felt after waking",
    "这段补觉结束，醒来后精力有所恢复，可以按意愿接回日常安排。":
        "The nap is over, her energy has recovered after waking, and she can get back to her routine as she likes.",
    "补觉后仍有一些疲惫": "She still felt a bit tired after the nap",
    "结束这一段补觉，先慢慢接回吃饭和轻活动": "Ended the nap and slowly eased back into eating and light activities",
    "补了一段觉，已经缓过来一些，仍适合放慢节奏。": "She has had a nap and feels somewhat better, but a slower pace still suits her.",
    "补觉期间又开始通信": "Messages started again during her nap",
    "结束这次未完整睡完的补觉，重新安排休息": "Ended the nap she hadn't finished and rearranged her rest",
    "这段补觉没有完整结束，当前已经醒着，之后再安排休息。": "The nap didn't finish; she is awake now and will arrange more rest later.",
    # meals
    "没有明显阻碍": "Nothing was in the way",
    "按自己的节奏吃完": "Finished eating at her own pace",
    "这一餐已经吃完。": "She has finished this meal.",
    "吃得比预想慢": "She ate more slowly than expected",
    "放慢速度，不急着接下一项": "Slowed down and didn't rush into the next thing",
    "这一餐吃完了，之后再接回安排。": "She has finished the meal and will get back to her plans afterwards.",
    "当下不想开始进食": "She didn't feel like eating right then",
    "决定这一餐先不吃": "Decided to skip this meal for now",
    "这次没有吃这一餐。": "She didn't have this meal.",
    "等待外卖送达": "Waiting for the delivery",
    "已经下单，等待送餐": "Placed the order and is waiting for it to arrive",
    "外卖已经下单，尚未送达，也没有开始吃。": "The delivery is ordered but hasn't arrived, and she hasn't started eating.",
    "餐食还需准备": "The meal still needed preparing",
    "开始准备这一餐": "Started preparing this meal",
    "正在准备餐食，还没有开始吃。": "She is preparing the meal and hasn't started eating.",
    "需要前往用餐地点": "She needed to get to where she is eating",
    "准备出门就餐": "Getting ready to go out to eat",
    "准备去计划中的地点吃饭，尚未到店。": "She is getting ready to go to the place she planned to eat at, and hasn't arrived yet.",
    "需要买饭带回": "She needed to pick up a meal",
    "准备去买这一餐": "Getting ready to go and buy this meal",
    "准备买饭带回，还没有取到餐。": "She is getting ready to buy a meal to bring back and hasn't picked it up yet.",
    "想先留一点间隔": "She wanted to leave a little gap first",
    "把用餐安排到已经选定的稍后时刻": "Scheduled the meal for the later time she had chosen",
    "这一餐尚未开始，保留明确的用餐计划。": "The meal hasn't started; the plan to eat is still set.",
    "停下其他活动，开始进食": "Stopped what else she was doing and started eating",
    "这一餐正在吃，尚未结束。": "She is eating this meal and hasn't finished.",
    "暂时没有很强的食欲": "She didn't have much of an appetite",
    "慢一点吃，不勉强赶进度": "Ate slowly and didn't force herself to hurry",
    "已经开始进食，仍在慢慢吃。": "She has started eating and is still taking her time.",
    # meanings and next steps
    "觉得这一小步有价值": "Feels this small step was worthwhile",
    "觉得进展受阻，有点在意": "Feels progress was blocked, and it bothers her a little",
    "觉得自己需要一点恢复空间": "Feels she needs a little room to recover",
    "把它当作普通的一段生活，没有额外解释": "Takes it as an ordinary stretch of life, with nothing more to read into it",
    "先留一段间隔再安排下一项": "Leave a gap before planning the next thing",
    "下次优先回到本次未稳的片段": "Next time, go back first to the passage that wasn't steady",
    "练习片段仍待巩固": "The passage still needs consolidating",
    "之后回到这次尚未解决的部分": "Come back later to the part that is still unresolved",
    "本次活动中留下的问题仍待处理": "A problem left over from this activity still needs dealing with",
}
# "{course}：{text}" and the course in the class motive.
COURSE_MOTIVE = re.compile(r"^跟进当前的(?P<course>.+)$")
NAP_MINUTES = re.compile(r"^开始补觉，计划休息(?P<minutes>\d+)分钟，醒来后再判断精力和接下来的安排。$")

# --- progress notes written by the program ---------------------------------------------------------------------------
PROGRESS: dict[str, str] = {
    "完成一遍慢速练习，仍有衔接需要处理": "Played through once slowly; some transitions still need work",
    "完成本次选定片段的连贯练习": "Played the chosen passage through smoothly",
    "当前困难小节仍未解决": "The difficult bars are still not solved",
    "整理了本次阅读的部分要点": "Summarized some of the main points of this reading",
    "读完本次选定内容并整理要点": "Finished the chosen reading and summarized the main points",
    "当前内容仍未理解清楚": "Still not clear on this material",
    "已整理一部分，仍有余项": "Part of it is tidied, with more left",
    "完成本次选定范围的整理": "Finished tidying the chosen area",
    "本次整理暂未完成": "The tidying isn't finished yet",
    "形成一版局部草稿，仍待修改": "Made a partial draft that still needs revising",
    "完成本次短草稿并留存": "Finished and saved a short draft",
    "本次草稿未达到预期": "The draft didn't turn out as hoped",
    "补充了一部分课堂笔记": "Added to her class notes",
    "完成本次选定知识点的笔记整理": "Finished organizing her notes on the chosen topic",
    "当前知识点仍有疑问": "Still has questions about this topic",
}
TASK_SUFFIX = " · 本次小任务"
PROJECTS: dict[str, str] = {
    "音乐与回忆个人研究": "Personal research on music and memory",
    "初始研究安排：探索音乐与回忆的联系；实际进展由后续生活记录更新。":
        "Starting research plan: exploring the link between music and memory. Actual progress is updated by later life records.",
    "肖邦《夜曲 Op.9 No.2》练习": "Practicing Chopin's Nocturne Op. 9 No. 2",
    "初始练习安排：持续练习肖邦《夜曲 Op.9 No.2》，关注音色；实际进展由后续生活记录更新。":
        "Starting practice plan: keep practicing Chopin's Nocturne Op. 9 No. 2, paying attention to tone. Actual progress is updated by later life records.",
    "初始学期安排已过期，待重新安排；没有完成记录，也没有实际开始或主动暂停的证据。paused仅表示旧计划不再自动沿用，实际进展未知。":
        "The starting semester plan has expired and is waiting to be rearranged. There is no record of completion and no evidence that it was started or deliberately paused. Paused only means the old plan is no longer carried forward automatically; actual progress is unknown.",
}
SUMMARIES: dict[str, str] = {
    "已完成语音回复": "Voice reply completed",
    "已完成翻唱回复": "Cover reply completed",
    "已完成音乐回复": "Music reply completed",
    "已确认分享日常视频": "Shared a clip of her day",
}
NEEDS: dict[str, str] = {
    "休息与恢复精力": "resting and recovering her energy",
    "按自己的意愿决定，维护个人边界": "deciding for herself and keeping her personal boundaries",
    "得到理解与陪伴": "being understood and having company",
    "受到尊重并被认真对待": "being respected and taken seriously",
    "弄清情况，减少误解": "getting clarity and reducing misunderstanding",
    "推进当前任务与生活安排": "moving her current tasks and plans forward",
    "确认自己或在意的人安好": "making sure she or someone she cares about is okay",
    "分享真实的生活与感受": "sharing her real life and feelings",
}

_TRAVEL = (
    ("准备前往", "", "Getting ready to go to {place}"),
    ("前往", "的路上", "On the way to {place}"),
)
_LOWER_KEEP = re.compile(r"^[A-Z]{2,}")


def _lower_first(text: str) -> str:
    return text if not text or _LOWER_KEEP.match(text) else text[0].lower() + text[1:]


def _upper_first(text: str) -> str:
    return text[:1].upper() + text[1:]


def english() -> bool:
    return ui_language.is_english()


# --- pieces -----------------------------------------------------------------------------------------------------------

def place_forms(name: object) -> tuple[str, str, str] | None:
    """(label, sentence form, bare noun phrase) of a place the program names itself or composes; else ``None``."""

    if not isinstance(name, str) or not name:
        return None
    if name in PLACES:
        return PLACES[name]
    if name in PLAN_PLACES:
        return PLAN_PLACES[name]
    if name.startswith("家里") and name[2:] in PLAN_PLACES:
        label, sentence, bare = PLAN_PLACES[name[2:]]
        return (f"{label} at home", f"{sentence} at home", f"{bare} at home")
    for prefix, suffix, template in _TRAVEL:
        if name.startswith(prefix) and name.endswith(suffix) and len(name) > len(prefix) + len(suffix):
            inner = name[len(prefix):len(name) - len(suffix)]
            bare = place_forms(inner)
            target = bare[2] if bare else inner
            text = template.format(place=target)
            return (text, text, _lower_first(text))
    return None


def _unknown_place(name: str) -> tuple[str, str, str]:
    return (name, "At " + name, name)


def focus_text(value: str) -> str:
    return FOCUSES.get(value, value)


def food_text(value: str) -> str:
    return FOODS.get(value, value)


def _meal(slot: str) -> str:
    return MEAL_SLOTS[slot]


def activity_forms(text: object) -> tuple[str, str] | None:
    """(title, sentence phrase) of an activity description the program composed; else ``None``."""

    if not isinstance(text, str) or not text:
        return None
    if text in ACTIVITIES:
        return ACTIVITIES[text]
    label, colon, focus = text.partition("：")
    if colon and label in ACTIVITIES and focus:
        title, phrase = ACTIVITIES[label]
        shown = focus_text(focus)
        return (f"{title}: {shown}", f"{phrase}: {shown}")
    if text.startswith("上") and text[1:] in COURSES:
        course = COURSES[text[1:]]
        return (f"In class: {course}", f"in class: {course}")
    for verb, (title, phrase) in MEAL_VERBS.items():
        if text.startswith(verb) and text[len(verb):] in MEAL_SLOTS:
            meal = _meal(text[len(verb):])
            return (title.format(meal=meal), phrase.format(meal=meal))
    for stage, (title, phrase) in MEAL_STAGES.items():
        if text.startswith(stage) and text[len(stage):] in {"早餐", "午餐", "晚餐"}:
            meal = _meal(text[len(stage):])
            return (title.format(meal=meal), phrase.format(meal=meal))
    return None


def activity_title(text: object) -> object:
    forms = activity_forms(text)
    return forms[0] if forms else text


def location_text(text: object) -> object:
    forms = place_forms(text)
    return forms[0] if forms else text


def _meal_note(text: str) -> str | None:
    """``<description>：<food>。`` (or ``<description>。``) as a meal's note."""

    if not text.endswith("。"):
        return None
    alone = activity_forms(text[:-1])
    if alone is not None:
        return f"{alone[0]}."
    head, colon, food = text[:-1].partition("：")
    if not colon or not food:
        return None
    forms = activity_forms(head)
    if forms is None and head in MEAL_STAGE_ALONE:
        forms = MEAL_STAGE_ALONE[head]
    if forms is None:
        return None
    return f"{forms[0]}: {food_text(food)}."


def _standalone_note(text: str) -> str | None:
    """``在<known place><known activity>。`` found without the record it came from (a quote of her own note)."""

    if not text.startswith("在") or not text.endswith("。"):
        return None
    body = text[1:-1]
    for cut in range(1, len(body)):
        where, forms = place_forms(body[:cut]), activity_forms(body[cut:])
        if where is not None and forms is not None:
            return f"{where[1]}, {forms[1]}."
    return None


def note_text(text: object, location: object = None, activity: object = None) -> object:
    """The note of a moment: ``在<place><activity>。`` or a meal's ``<description>：<food>。``."""

    if not isinstance(text, str) or not text:
        return text
    if text in EPISODES:
        return EPISODES[text]
    meal = _meal_note(text)
    if meal is not None:
        return meal
    forms = activity_forms(activity)
    if forms is None:
        return _standalone_note(text) or text
    if not text.startswith("在") or not text.endswith(activity + "。"):
        return text
    name = text[1:len(text) - len(activity) - 1]
    if isinstance(location, str) and location and name != location:
        return text
    where = place_forms(name) or _unknown_place(name)
    return f"{where[1]}, {forms[1]}."


def _course_prefixed(text: str) -> str | None:
    course, colon, rest = text.partition("：")
    if colon and course in COURSES and rest:
        inner = EPISODES.get(rest)
        if inner is not None:
            return f"{COURSES[course]}: {inner}"
    return None


def episode_text(text: object) -> object:
    """A motive, obstacle, response, result, meaning or next step of a stock episode."""

    if not isinstance(text, str) or not text:
        return text
    if text in EPISODES:
        return EPISODES[text]
    motive = COURSE_MOTIVE.match(text)
    if motive and motive["course"] in COURSES:
        return f"Following the current class: {COURSES[motive['course']]}"
    nap = NAP_MINUTES.match(text)
    if nap:
        return (f"She has started a nap, planning to rest for {nap['minutes']} minutes and then see how her energy "
                "is and what comes next.")
    return _course_prefixed(text) or text


UNCLEAR_PART = re.compile(r"^(?P<activity>.+)时，有一部分内容还没理解；具体疑问尚未记录。$")
CONCERNS: dict[str, str] = {
    "这一处暂时没弄懂，不代表整节课失败或已经结束。":
        "She hasn't got this part yet, which doesn't mean the whole class went badly or is over.",
}


def concern_text(text: object) -> object:
    """A line of what is on her mind that the program composed from a record, or a quote of one of her notes."""

    if not isinstance(text, str) or not text:
        return text
    if text in CONCERNS:
        return CONCERNS[text]
    unclear = UNCLEAR_PART.match(text)
    if unclear and unclear["activity"].startswith("上") and unclear["activity"][1:] in COURSES:
        course = COURSES[unclear["activity"][1:]]
        return f"Part of {course} wasn't clear yet; the specific questions haven't been noted."
    return note_text(text)


def _progress_text(text: str) -> str:
    return PROGRESS.get(text, text)


def project_title(text: object) -> object:
    if not isinstance(text, str):
        return text
    if text in PROJECTS:
        return PROJECTS[text]
    if text.endswith(TASK_SUFFIX):
        focus = text[:-len(TASK_SUFFIX)]
        return f"{_upper_first(focus_text(focus))} · a small task for now"
    return text


_NEXT = re.compile(r"^(?P<place>住处附近|家里|住处|学校|店里)(?P<action>.+)$")


def _next_step(text: str) -> str | None:
    match = _NEXT.match(text)
    if not match:
        return None
    forms = activity_forms(match["action"])
    if forms is None:
        return None
    where = place_forms(match["place"])
    return f"{where[1]}, {forms[1]}"


def project_detail(text: object) -> object:
    """A project's detail: a fixed plan sentence, or ``本次：…。本次变化：…。下一步打算在…。`` composed by the program."""

    if not isinstance(text, str) or not text:
        return text
    if text in PROJECTS:
        return PROJECTS[text]
    if not text.startswith("本次：") or not text.endswith("。"):
        return text
    parts = text[:-1].split("。")
    out = []
    for part in parts:
        if part.startswith("本次："):
            forms = activity_forms(part[len("本次："):])
            if forms is None:
                return text
            out.append(f"This time: {forms[0]}.")
        elif part.startswith("本次变化："):
            out.append(f"What changed: {_progress_text(part[len('本次变化：'):])}.")
        elif part.startswith("下一步打算在"):
            step = _next_step(part[len("下一步打算在"):])
            if step is None:
                return text
            out.append(f"Planned next, not done yet: {_lower_first(step)}.")
        else:
            return text
    return " ".join(out)


# --- the payloads -----------------------------------------------------------------------------------------------------

def _current(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    out = dict(item)
    location, activity, note = item.get("location"), item.get("activity"), item.get("note")
    if "note" in item:
        out["note"] = note_text(note, location, activity)
    if "activity" in item:
        out["activity"] = activity_title(activity)
    if "location" in item:
        out["location"] = location_text(location)
    return out


def _rhythm(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    out = dict(value)
    for key in ("activity", "note"):
        if isinstance(out.get(key), str):
            out[key] = RHYTHM.get(out[key], out[key])
    wellbeing = out.get("wellbeing")
    if isinstance(wellbeing, dict) and isinstance(wellbeing.get("summary"), str):
        out["wellbeing"] = {**wellbeing, "summary": RHYTHM.get(wellbeing["summary"], wellbeing["summary"])}
    return out


def _lesson(item: Any) -> Any:
    if isinstance(item, dict) and isinstance(item.get("title"), str):
        return {**item, "title": COURSES.get(item["title"], item["title"])}
    return item


def _schedule(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    out = dict(value)
    if isinstance(out.get("classes"), list):
        out["classes"] = [_lesson(item) for item in out["classes"]]
    for key in ("current_class", "next_class"):
        if key in out:
            out[key] = _lesson(out[key])
    return out


def _today_activity(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    out = dict(item)
    if "note" in item:
        out["note"] = note_text(item["note"], None, item.get("activity"))
    if "activity" in item:
        out["activity"] = activity_title(item["activity"])
    return out


def _meal_record(item: Any) -> Any:
    if isinstance(item, dict) and isinstance(item.get("food"), str):
        return {**item, "food": food_text(item["food"])}
    return item


def _episode(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    out = dict(item)
    trigger = item.get("trigger")
    if isinstance(trigger, dict) and "detail" in trigger:
        out["trigger"] = {**trigger, "detail": episode_text(trigger["detail"])}
    if isinstance(item.get("process"), list):
        out["process"] = [
            {key: episode_text(value) if key in {"obstacle", "response", "outcome"} else value
             for key, value in step.items()} if isinstance(step, dict) else step
            for step in item["process"]
        ]
    result = item.get("result")
    if isinstance(result, dict) and "detail" in result:
        out["result"] = {**result, "detail": episode_text(result["detail"])}
    meaning = item.get("interpretation")
    if isinstance(meaning, dict) and "meaning" in meaning:
        out["interpretation"] = {**meaning, "meaning": episode_text(meaning["meaning"])}
    effects = item.get("effects")
    if isinstance(effects, dict):
        out["effects"] = {key: episode_text(value) if key in {"next_action", "open_loop"} else value
                          for key, value in effects.items()}
    return out


def _world(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    out = dict(value)
    if "schedule" in value:
        out["schedule"] = _schedule(value["schedule"])
    for key, convert in (("today_activities", _today_activity), ("meals", _meal_record), ("recent_episodes", _episode)):
        if isinstance(value.get(key), list):
            out[key] = [convert(item) for item in value[key]]
    return out


def _project(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    out = dict(item)
    if "title" in item:
        out["title"] = project_title(item["title"])
    for key in ("detail", "progress"):
        if isinstance(item.get(key), str):
            out[key] = project_detail(item[key]) if key == "detail" else _progress_text(item[key])
    if isinstance(item.get("history"), list):
        out["history"] = [_project(entry) for entry in item["history"]]
    return out


def _moment_content(kind: Any, content: Any) -> Any:
    if not isinstance(content, dict):
        return content
    out = dict(content)
    if kind == "daily":
        out.update(_current({key: content[key] for key in ("location", "activity", "note") if key in content}))
    delivery = content.get("delivery")
    if isinstance(delivery, dict) and isinstance(delivery.get("summary"), str):
        out["delivery"] = {**delivery, "summary": SUMMARIES.get(delivery["summary"], delivery["summary"])}
    for key in ("progress", "updates"):
        if isinstance(content.get(key), list):
            out[key] = [_project(entry) for entry in content[key]]
    return out


def _moment(item: Any) -> Any:
    if not isinstance(item, dict) or "content" not in item:
        return item
    return {**item, "content": _moment_content(item.get("kind"), item["content"])}


def _reaction(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    out = dict(item)
    if isinstance(item.get("goal_or_need"), str):
        out["goal_or_need"] = NEEDS.get(item["goal_or_need"], item["goal_or_need"])
    if isinstance(item.get("quote"), str):
        out["quote"] = concern_text(item["quote"])
    return out


def _emotion(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    out = dict(value)
    if isinstance(value.get("reactions"), list):
        out["reactions"] = [_reaction(item) for item in value["reactions"]]
    if isinstance(value.get("concerns"), list):
        out["concerns"] = [
            {**item, "summary": concern_text(item["summary"])}
            if isinstance(item, dict) and isinstance(item.get("summary"), str) else item
            for item in value["concerns"]
        ]
    return out


def _reply_basis(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    out = dict(value)
    if isinstance(value.get("activity"), str):
        out["activity"] = activity_title(value["activity"])
    if isinstance(value.get("reactions"), list):
        out["reactions"] = [_reaction(item) for item in value["reactions"]]
    if isinstance(value.get("concerns"), list):
        out["concerns"] = [concern_text(item) for item in value["concerns"]]
    return out


def localize_life(value: Any) -> Any:
    """The life payload of the World page with the program's own wording in English; Chinese mode returns ``value``."""

    if not english() or not isinstance(value, dict):
        return value
    out = dict(value)
    if "current" in value:
        out["current"] = _current(value["current"])
    if "rhythm" in value:
        out["rhythm"] = _rhythm(value["rhythm"])
    if "world" in value:
        out["world"] = _world(value["world"])
    for key in ("projects", "shared"):
        if isinstance(value.get(key), list):
            out[key] = [_project(item) for item in value[key]]
    if isinstance(value.get("moments"), list):
        out["moments"] = [_moment(item) for item in value["moments"]]
    if "emotion" in value:
        out["emotion"] = _emotion(value["emotion"])
    if "reply_basis" in value:
        out["reply_basis"] = _reply_basis(value["reply_basis"])
    return out


def localize_moments(value: Any) -> Any:
    """The older-moments page of the World page; Chinese mode returns ``value``."""

    if not english() or not isinstance(value, dict) or not isinstance(value.get("moments"), list):
        return value
    return {**value, "moments": [_moment(item) for item in value["moments"]]}


__all__ = ["localize_life", "localize_moments"]
