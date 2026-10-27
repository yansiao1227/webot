"""仅处理精确的出站元数据，不读取或补充聊天历史。

输入来自采集到的原生 Record，或本次提交观察到的 Task。
所有端点都不接受调用方自行选择的路径、账号、会话或排序键。
"""
import hashlib
import re
import time

SOURCE = 'weixin41930_message_db_exact_sort_seq_v1'
TASK_SOURCE = 'weixin41930_send_task_record110_v1'


def outgoing_rows(connection, account, conversation, sort_seq, created=None, message_type='text'):
    table='Msg_'+hashlib.md5(conversation.encode()).hexdigest()
    if not connection.execute('SELECT 1 FROM sqlite_master WHERE type=? AND name=?',('table',table)).fetchone():
        return []
    rows=connection.execute(f'SELECT local_id,server_id,sort_seq,create_time,real_sender_id,local_type FROM "{table}" WHERE sort_seq=?',
                            (int(sort_seq),)).fetchmany(2)
    if not rows:
        return []
    owners=connection.execute('SELECT rowid FROM Name2Id WHERE user_name=?',(account,)).fetchmany(2)
    if len(rows)!=1 or len(owners)!=1:
        raise ValueError('native identity ambiguous')
    local_id,server_id,sequence,creation,sender,kind=rows[0]
    allowed={'text':{1},'reference':{49},'image':{3,13,39},'voice':{34},'video':{43,44,62},
             'file':{49},'sticker':{47},'card':{42,49,66,67}}
    if (any(type(value) is not int for value in rows[0]) or local_id<=0 or creation<=0 or sequence!=int(sort_seq)
            or sender!=owners[0][0] or (message_type in allowed and kind&0xffffffff not in allowed[message_type])
            or (created is not None and creation!=created)):
        raise ValueError('native outgoing identity mismatch')
    return [{'native_local_id':str(local_id),'message_id':str(server_id & ((1<<64)-1)) if server_id else None,
             'native_sort_seq':str(sequence),'native_created_at':creation,'native_message_id_source':SOURCE,
             'native_message_id_state':'ready' if server_id else 'pending'}]


def outgoing_metadata(account, conversation, sort_seq, created=None, message_type='text'):
    from .native_assets import account_directory
    from .native_media import media_snapshot
    if (not isinstance(sort_seq,str) or not re.fullmatch(r'[1-9][0-9]{0,18}',sort_seq)
            or int(sort_seq)>=(1<<63) or not isinstance(conversation,str)
            or not re.fullmatch(r'(?:wxid_[A-Za-z0-9_-]+|[A-Za-z0-9_-]+@chatroom)',conversation)):
        raise ValueError('invalid native identity')
    directory=account_directory(account)/'db_storage/message'
    files=sorted(p for p in directory.glob('message_*.db') if re.fullmatch(r'message_[0-9]+[.]db',p.name))
    if not files or len(files)>4 or any(p.is_symlink() for p in files):
        raise ValueError('native identity shard limit')
    results=[];deadline=time.monotonic()+6
    for path in files:
        if time.monotonic()>=deadline:
            raise TimeoutError('native identity lookup timeout')
        connection=media_snapshot(path,account)
        try:
            results.extend(outgoing_rows(connection,account,conversation,sort_seq,created,message_type))
        finally:
            connection.close()
    if time.monotonic()>=deadline:
        raise TimeoutError('native identity lookup timeout')
    if len(results)>1:
        raise ValueError('native identity ambiguous across shards')
    return results[0] if results else {'native_message_id_state':'pending'}


def enrich_outgoing(result, account, conversation, created=None):
    """身份解析失败不能把已接受的发送改为失败。"""
    try:
        kind=result.get('message_type','text')
        if result.get('reference',{}).get('source')=='weixin41930_native_reference_v1':
            kind='reference'
        metadata=outgoing_metadata(account,conversation,result['native_sort_seq'],created,kind)
        if result.get('message_id') and metadata.get('message_id') not in (None,result['message_id']):
            raise ValueError('native server identity mismatch')
        return {**result,**{k:v for k,v in metadata.items() if v is not None}}
    except Exception as error:
        # 诊断信息不得包含密钥、数据库内容或入站正文。
        return {**result,'native_message_id_state':'unavailable','native_identity_error':type(error).__name__}
