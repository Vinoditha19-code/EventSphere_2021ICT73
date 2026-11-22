"""A local, synchronous JSON message bus backed by durable SQLite inboxes.

Only the bus invokes an agent's handle method. Agents communicate by exchanging
JSON envelopes through ask/broadcast, never by calling another agent directly.
The caller owns the surrounding database transaction.
"""
import json
from uuid import uuid4

PERFORMATIVES = {'REQUEST', 'AGREE', 'REFUSE', 'PROPOSE', 'INFORM', 'FAILURE'}


class MessageBus:
    def __init__(self, db, now):
        self.db = db
        self.now = now
        self.agents = {}
        self.depth = 0

    def register(self, agent):
        self.agents[agent.name] = agent

    def envelope(self, sender, receiver, performative, content, conversation_id=None):
        message = {
            'message_id': str(uuid4()),
            'conversation_id': conversation_id or str(uuid4()),
            'sender': sender,
            'receiver': receiver,
            'performative': performative,
            'content': content,
            'timestamp': self.now.isoformat()
        }
        # Serialization gives receivers a separate JSON value, not a shared object.
        return json.loads(json.dumps(message, allow_nan=False))

    def log(self, message):
        self.db.execute('INSERT INTO agent_messages(message) VALUES (?)', (json.dumps(message),))

    def deliver(self, message):
        required = {'message_id', 'conversation_id', 'sender', 'receiver', 'performative', 'content'}
        if not required <= message.keys() or message['performative'] not in PERFORMATIVES:
            raise ValueError('Invalid agent message envelope')
        old = self.db.execute('SELECT reply FROM agent_inbox WHERE message_id = ?',
                              (message['message_id'],)).fetchone()
        if old and old['reply']:
            return json.loads(old['reply'])
        if self.depth >= 30:
            raise RuntimeError('Agent message depth exceeded')
        if not old:
            self.log(message)
            self.db.execute('INSERT INTO agent_inbox(message_id,payload) VALUES (?,?)',
                            (message['message_id'], json.dumps(message)))
        self.depth += 1
        try:
            agent = self.agents.get(message['receiver'])
            if agent is None:
                kind, content = 'FAILURE', {'reason': 'Unknown agent', 'http_status': 500}
            else:
                kind, content = agent.handle(json.loads(json.dumps(message)), self)
            if kind not in PERFORMATIVES:
                raise ValueError('Invalid reply performative')
            if kind in {'REFUSE', 'PROPOSE', 'FAILURE'} and not content.get('reason'):
                raise ValueError('Refusals and proposals must explain their reason')
            reply = self.envelope(message['receiver'], message['sender'], kind,
                                  content, message['conversation_id'])
            reply['in_reply_to'] = message['message_id']
            self.log(reply)
            self.db.execute("UPDATE agent_inbox SET status='done', reply=? WHERE message_id=?",
                            (json.dumps(reply), message['message_id']))
            return reply
        finally:
            self.depth -= 1

    def ask(self, sender, receiver, content, conversation_id=None):
        return self.deliver(self.envelope(sender, receiver, 'REQUEST', content, conversation_id))

    def broadcast(self, sender, content, conversation_id):
        event = self.envelope(sender, '*', 'INFORM', content, conversation_id)
        self.log(event)
        # Each subscribed listener receives its own durable message.
        for name in ('WaitlistAgent', 'NotificationAgent'):
            if name != sender:
                message = self.envelope(sender, name, 'INFORM', content, conversation_id)
                message['broadcast_id'] = event['message_id']
                self.deliver(message)

    def recover(self):
        rows = self.db.execute("SELECT payload FROM agent_inbox WHERE status='pending' ORDER BY sequence").fetchall()
        for row in rows:
            self.deliver(json.loads(row['payload']))
