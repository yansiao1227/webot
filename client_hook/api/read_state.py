"""Native conversation read intent, separate from consumer acknowledgements."""
import hashlib
import json
import threading
import time
from .errors import ApiError
from .store import encode

MENTION_SOURCE='weixin41930_record_source_180'
def allowed(event, account):
    mentions=event.get('mentions') or {}
    return (event.get('account')==account and event.get('direction')=='incoming' and
        event.get('message_type')=='text' and
        (not event.get('conversation_id','').endswith('@chatroom') or
         (mentions.get('state')=='known' and mentions.get('source')==MENTION_SOURCE and
          account in mentions.get('wxids',[]))))

def snapshot(account,chat):
    from .native_assets import account_directory
    from .native_media import media_snapshot
    path=account_directory(account)/'db_storage/session/session.db'
    connection=media_snapshot(path,account)
    try:
        rows=connection.execute('''SELECT unread_count,unread_first_msg_srv_id,
            last_clear_unread_timestamp,last_msg_locald_id,last_timestamp
            FROM SessionTable WHERE username=? LIMIT 2''',(chat,)).fetchall()
        if len(rows)!=1:raise ApiError(503,'native_read_session_unresolved','未能唯一定位原生会话')
        return dict(zip(('unread_count','first_unread_message_id','last_clear_unread_timestamp',
                         'last_msg_local_id','last_timestamp'),rows[0]))
    finally:connection.close()

class ReadState:
    def __init__(self,store,resources,backend):
        self.store,self.resources,self.backend=store,resources,backend
        self.lock=threading.RLock();self.workers={}
        with store.transaction():
            store.db.execute('''CREATE TABLE IF NOT EXISTS read_operations(
                event_id TEXT PRIMARY KEY,account TEXT NOT NULL,created REAL NOT NULL,
                state TEXT NOT NULL,result TEXT NOT NULL)''')
            store.db.execute("UPDATE read_operations SET state='unknown' WHERE state='running'")

    def verified(self):
        with self.store.condition:
            return self.store.db.execute("SELECT 1 FROM read_operations WHERE state='confirmed' LIMIT 1").fetchone() is not None

    def status(self,event_id,account):
        with self.store.condition:
            row=self.store.db.execute('SELECT * FROM read_operations WHERE event_id=? AND account=?',
                (event_id,account)).fetchone()
            if not row:raise ApiError(404,'read_operation_not_found','无已读操作')
            return {'event_id':event_id,'state':row['state'],**json.loads(row['result'])}

    def request(self,event_id,account):
        event=self.resources.event(event_id,account)
        if not allowed(event,account):raise ApiError(403,'read_trigger_denied','仅合法入站文字指令可标记会话已读')
        with self.lock,self.store.transaction():
            row=self.store.db.execute('SELECT state FROM read_operations WHERE event_id=?',(event_id,)).fetchone()
            if not row:
                if len(self.workers)>=2:raise ApiError(429,'read_busy','已读操作并发已满')
                self.store.db.execute('INSERT INTO read_operations VALUES(?,?,?,?,?)',
                    (event_id,account,event['captured_at'],'running','{}'))
                worker=threading.Thread(target=self._run,args=(event,),daemon=True)
                self.workers[event_id]=worker;worker.start()
        return self.status(event_id,account)

    def _run(self,event):
        eid=event['event_id'];account=event['account'];chat=event['conversation_id']
        state='unknown';result={}
        try:
            if self.backend.__class__.__name__!='NativeBackend' or self.backend.args.fixture_pid is not None:
                raise ApiError(503,'native_read_unavailable','当前后端未验证原生已读')
            self.backend.profile()
            before=snapshot(account,chat);result['before']=before
            if before['unread_count']==0:
                state='already_read'
            else:
                from hook_runtime.read_rpc import invoke
                request_id='read_'+hashlib.sha256((account+'\0'+eid).encode()).hexdigest()[:56]
                # Durable intent precedes RPC. Unknown submissions are never retried.
                result['request_id']=request_id
                native=invoke(account,chat,request_id);result['native']=native
                if native['error'] or native['thread_error'] or not native['submitted']:
                    raise ApiError(503,'native_read_not_submitted','原生已读未确认提交')
                state='requested'
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    after=snapshot(account,chat);result['after']=after
                    # A zero count verifies actual local state; newer messages may
                    # arrive immediately afterward and become unread normally.
                    if (after['unread_count']==0 and after['last_clear_unread_timestamp']>=
                            before['last_clear_unread_timestamp']):
                        state='confirmed'
                        break
                    time.sleep(.15)
        except Exception as error:
            result['error']=getattr(error,'code',type(error).__name__)
        finally:
            with self.lock,self.store.transaction():
                self.store.db.execute('UPDATE read_operations SET state=?,result=? WHERE event_id=?',
                    (state,encode(result),eid));self.workers.pop(eid,None)

    def prune(self):
        with self.store.transaction():
            self.store.db.execute("DELETE FROM read_operations WHERE created<? AND state!='running'",
                (self.store.clock()-self.store.retention,))

    def close(self):
        for worker in list(self.workers.values()):
            worker.join(15)
            if worker.is_alive():
                raise RuntimeError('原生已读线程仍执行中，保留数据库')
