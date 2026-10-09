"""QQ transport contract for server-owned story/ASMR generation."""
import asyncio
from pathlib import Path
import re
import hashlib
import time

_capabilities = {}

BEDTIME_OFFER_INSTRUCTION = {
    'bedtime': """<bedtime_audio_offer>
本轮冻结JEV决定是bedtime：用户本人现在准备睡觉，本次睡前对话尚未邀请且没有拒绝。先自然回应，然后必须用一句直接问句询问想听睡前故事还是轻声ASMR陪伴。不能只说晚安或“想听的话告诉我”，不再判断是否需要邀请。午睡也在此刻询问，不能推迟成“醒了/以后想听再告诉我”。问句要让对方听出这是她录给对方的一段语音，可以自然表达为“要不要我录一段给你听？讲个睡前故事，还是小声哄你睡？”只问内容选择，不问几分钟；时长由应用采用默认值。沿用当前人格与关系，不编造近况，不复述用户原话当作自己的经历。本轮需要回应，skip=false，省略silence字段。这是询问，speech=null，不承诺制作或发送，不创建followup。继续输出原聊天JSON。
</bedtime_audio_offer>""",
    'clarify': """<bedtime_audio_offer>
本轮冻结JEV决定是clarify：用户答应了故事和ASMR两个选项的邀请，但尚未选择。简短直接问是想听她讲个睡前故事，还是小声哄睡陪着，不替用户选择，不问时长。本轮需要回应，skip=false，省略silence字段。这一步speech=null，不承诺制作，不创建followup。继续输出原聊天JSON。
</bedtime_audio_offer>""",
    'none': """<bedtime_audio_offer>
本轮冻结JEV决定是none：当前无需睡前音频邀请或选择澄清。按普通聊天回应最后一条用户消息，不主动邀请故事或ASMR，不复述历史邀请，speech=null，不新增followup。尊重拒绝和不用回复；silence.evidence从当前原话逐字摘抄，不改写。你的近况不是用户的经历。继续输出原聊天JSON。
</bedtime_audio_offer>"""
}


SPEECH_WRITER_INSTRUCTION = """<long_audio_task>
本轮JEV已确认用户现在要一段长音频，见speech_request：mode=story是正常声音讲故事，asmr是在耳边轻声陪伴、哄睡而不讲故事，asmr_story是耳语讲故事；target_seconds是目标时长（秒）。
必须在同一个JSON里额外输出speech={"title":"音频标题，20字内","spoken_text":"将被录成音频的完整正文","continuation_summary":"故事梗概，供以后续讲，200字内；asmr填空字符串"}。
text只写一两句简短接话，比如答应下来、让对方躺好等一会儿；故事或陪伴的正文只写在speech.spoken_text里，text不重复正文，也不声称已经录好或发出。
spoken_text按target_seconds写够长度，字数不算标点：正常讲故事每秒约4.5个字，耳语每秒约3.5个字（例如180秒的故事约810字，600秒的耳语约2100字）。宁可写长一些，太短会录得比约定的短。只写她要说出口的话，用自然口语和标点表现停顿；不写（轻声）（停顿）[耳语]【动作】这类括号提示、音效或旁白说明，不以{、[或```开头。
故事是虚构的，不当成她现实里发生的事；ASMR是她贴近耳边、慢慢地、温柔地陪对方放松入睡的话，可以有呼吸般的停顿和重复，不讲完整故事。沿用当前人格和关系，不编造双方的现实经历。
</long_audio_task>"""


async def supported(environment):
    """Cache server readiness; older deployments keep ordinary chat working."""
    from runtime.remote_generation import RemoteGeneration
    url, token = environment.get('OLIVIA_GPU_API_URL',''), environment.get('OLIVIA_GPU_API_KEY','')
    if not url or not token:
        return False
    key = (url, hashlib.sha256(token.encode()).hexdigest())
    cached = _capabilities.get(key)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    try:
        caps = await asyncio.wait_for(RemoteGeneration(url,token).request('capabilities',{}),3)
        enabled = caps.get('speech_experience') is True
    except Exception:
        enabled = False
    _capabilities.clear()
    _capabilities[key]=(time.monotonic()+60,enabled)
    return enabled


def validate_intent(value):
    if value is None:
        return None
    if (not isinstance(value,dict) or set(value)-{'ambience_scene'}!={'mode','target_seconds','continuation'}
            or value['mode'] not in ('story','asmr','asmr_story')
            or type(value['target_seconds']) is not int or not 60<=value['target_seconds']<=600
            or type(value['continuation']) is not bool
            or value.get('ambience_scene','none') not in ('none','quiet_room','rain_room','bedside_reading','desk_writing','seaside')):
        raise ValueError('SPEECH_INTENT_INVALID')
    return value


def validate_script(value):
    if value is None:
        return None
    if isinstance(value,dict) and isinstance(value.get('spoken_text'),str):
        value={**value,'spoken_text':value['spoken_text'].replace('\\n','\n').replace('\\"','"')}
    if (not isinstance(value,dict) or set(value)!={'title','spoken_text','continuation_summary'}
            or not isinstance(value['title'],str) or not 1<=len(value['title'])<=80
            or not isinstance(value['spoken_text'],str) or not 40<=len(value['spoken_text'])<=8000
            or not isinstance(value['continuation_summary'],str) or len(value['continuation_summary'])>1200
            or value['spoken_text'].lstrip().startswith(('{','[','```'))
            or re.search(r'\[\[|<\||\[(?:耳语|停顿|动作)|【(?:耳语|停顿|动作)|[（(](?:轻声|耳语|靠近|极轻|停顿|音效)',value['spoken_text'])):
        raise ValueError('SPEECH_SCRIPT_INVALID')
    return value


def filename(title):
    value=re.sub(r'[\x00-\x1f<>:"/\\|?*]','_',title).strip(' .')[:70] or '睡前音频'
    if value.upper() in {'CON','PRN','AUX','NUL',*[f'COM{i}' for i in range(1,10)],*[f'LPT{i}' for i in range(1,10)]}:
        value='音频_'+value
    return value+'.mp3'


async def deliver(server,row,send):
    from .backend import persist_chat
    from runtime.remote_generation import RemoteGeneration
    from runtime.media.music_reply import _media_duration_seconds
    import os
    state=row.get('speech_delivery_status')
    if state in {'SENDING','UNKNOWN','DELIVERED'}:
        return  # ACK ambiguity requires reconciliation, never an automatic resend.
    script=validate_script(row['speech_script'])
    seal = row.get('content_review')
    if seal is not None:
        if (not isinstance(seal, dict) or seal.get('version') != 1
                or not isinstance(seal.get('hashes'), dict)
                or seal['hashes'].get('speech') != hashlib.sha256(script['spoken_text'].encode()).hexdigest()):
            raise ValueError('SPEECH_REVIEW_CONTENT_CHANGED')
    intent=validate_intent(row['speech_intent'])
    directory=server._state_root()/'media'/'speech'/row['letter_id']
    directory.mkdir(parents=True,exist_ok=True)
    output=directory/filename(script['title'])
    api=RemoteGeneration(os.environ.get('OLIVIA_GPU_API_URL',''),os.environ.get('OLIVIA_GPU_API_KEY',''))
    row.update(speech_status='GENERATING')
    await persist_chat(server)
    def validate(path):
        seconds=_media_duration_seconds(path,required_streams=('0:a:0',))
        if not seconds or seconds<=0:
            raise ValueError('SPEECH_AUDIO_INVALID')
    if not row.get('speech_task_id'):
        task=await api.generate('tts',dict(channel='qq',text=script['spoken_text'],
             speech_title=script['title'],speech_mode=intent['mode'],target_seconds=intent['target_seconds'],
             **({'ambience_scene':intent['ambience_scene']} if 'ambience_scene' in intent else {})),
             output,receipt_path=directory/'request.json',timeout=7200,validate=validate)
        row.update(speech_task_id=task['task_id'],speech_result=task.get('speech'))
    else:
        result=row.get('speech_result') or {}
        valid=output.is_file()
        if valid and result.get('sha256'):
            valid=(output.stat().st_size==result.get('bytes')
                   and hashlib.sha256(output.read_bytes()).hexdigest()==result['sha256'])
        if not valid:
            task=await api.download_task(row['speech_task_id'],output,validate=validate)
            row['speech_result']=task.get('speech',result)
    validate(output)
    row.update(speech_status='READY',speech_file=str(output),speech_delivery_status='SENDING')
    await persist_chat(server)  # Reserve the outbound operation before touching QQ.
    try:
        receipt=await send.file(output,output.name)
        if not receipt:
            raise ValueError('QQ_FILE_ACK_INVALID')
    except Exception as exc:
        from .qq import QQFileRejected
        if isinstance(exc, QQFileRejected):
            row['speech_delivery_status']='FAILED'
            await persist_chat(server)
            raise
        row['speech_delivery_status']='UNKNOWN'
        await persist_chat(server)
        raise
    row.update(speech_delivery_status='DELIVERED',speech_delivery_receipt=str(receipt))
    await persist_chat(server)
    # Retain the server result until QQ confirms file acceptance. Otherwise a
    # failed send plus missing local file could no longer recover this order.
    from runtime.gpu_cleanup import acknowledge_result
    await acknowledge_result(api,row['speech_task_id'],output)
    # Fiction stays in the separate continuation field; ordinary memory/world consumers
    # see only reply_text, never the spoken story or its fictional events.
