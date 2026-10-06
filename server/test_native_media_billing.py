import asyncio
import base64
from io import BytesIO
import json
import wave

from asgiref.testing import ApplicationCommunicator
from django.test import TransactionTestCase, override_settings
import httpx

from qwen.async_relay import Relay, db
from qwen.models import AccessKey, Usage
from qwen.quota import issue_key, grant_money
from qwen.pricing import metered_units, with_minimum
from qwen.relay_models import retail_rates


@override_settings(RELAYJETTY_API_KEY='synthetic-provider', RELAYJETTY_BASE_URL='https://example.invalid/v1')
class NativeBillingTests(TransactionTestCase):
    def setUp(self):
        self.account,self.key=issue_key('synthetic-native-media')
        grant_money(self.account.pk,100_000_000,'synthetic-credit','test')
        raw=BytesIO()
        with wave.open(raw,'wb') as output:
            output.setparams((1,2,16000,0,'NONE','not compressed'));output.writeframes(b'\0\0'*16000)
        self.payload={'kind':'audio','mime_type':'audio/wav','data':base64.b64encode(raw.getvalue()).decode()}

    async def call(self,relay,payload=None,key=None,request_id='media-observation:synthetic'):
        connection=ApplicationCommunicator(relay,{'type':'http','method':'POST','path':'/v1/media/observations',
            'headers':[(b'authorization',('Bearer '+(key or self.key)).encode()),(b'idempotency-key',request_id.encode())]})
        await connection.send_input({'type':'http.request','body':json.dumps(payload or self.payload).encode()})
        start=await connection.receive_output(timeout=5)
        body=await connection.receive_output(timeout=5)
        await connection.wait(timeout=5)
        return start['status'],json.loads(body['body'])

    async def test_native_route_settles_once_without_freezing_and_blocks_duplicate_or_invalid_media(self):
        calls=[]
        async def upstream(request):
            calls.append(request)
            self.assertEqual(request.headers['authorization'],'Bearer synthetic-provider')
            account=await db(AccessKey.objects.get,pk=self.account.pk)
            self.assertEqual(account.held_units,0)
            return httpx.Response(200,json={'candidates':[{'finishReason':'STOP','content':{'parts':[{'text':'你好，十七元'}]}}],
                'usageMetadata':{'promptTokenCount':35,'candidatesTokenCount':8,'thoughtsTokenCount':5,'totalTokenCount':48}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            relay=Relay(None,client=client)
            status,result=await self.call(relay)
            self.assertEqual((status,result),(200,{'kind':'audio','summary':'你好，十七元'}))
            self.assertEqual((await self.call(relay))[0],409)
            self.assertEqual((await self.call(relay,{**self.payload,'data':'broken!'},request_id='invalid'))[0],400)
            self.assertEqual((await self.call(relay,key='wrong',request_id='foreign'))[0],401)
        row=await db(Usage.objects.get,account=self.account)
        version,ir,ort=retail_rates('gemini-3.8-flash')
        self.assertEqual(row.charged_units,with_minimum(metered_units(35*ir+13*ort,version),version))
        self.assertEqual((row.prompt_tokens,row.completion_tokens,row.reserved_units,row.status),(35,13,0,'settled'))
        self.assertEqual(len(calls),1)

    async def test_empty_or_truncated_answer_is_an_error_and_confirmed_usage_still_reconciles(self):
        for finish in ('STOP','MAX_TOKENS','MALFORMED_FUNCTION_CALL'):
            async def upstream(request):
                return httpx.Response(200,json={'candidates':[{'finishReason':finish,'content':{'parts':[]}}],
                    'usageMetadata':{'promptTokenCount':35,'candidatesTokenCount':0,'totalTokenCount':35}})
            async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
                status,result=await self.call(Relay(None,client=client),request_id='media-observation:'+finish)
                self.assertEqual(status,502)
                self.assertEqual(result['error']['code'],'media_observation_incomplete')
        self.assertEqual(await db(Usage.objects.filter(status='settled').count),3)

    async def test_explicit_provider_rejection_is_free_and_unknown_usage_is_never_invented(self):
        for status in (400,500):
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _:httpx.Response(status))) as client:
                result,_=await self.call(Relay(None,client=client),request_id='media-observation:'+str(status))
                self.assertEqual(result,502)
        rejected=await db(Usage.objects.get,request_id='media-observation:400')
        unknown=await db(Usage.objects.get,request_id='media-observation:500')
        self.assertEqual((rejected.charged_units,rejected.status),(0,'rejected'))
        self.assertIsNone(unknown.charged_units)
        self.assertIsNone(unknown.prompt_tokens)
        self.assertEqual(unknown.reserved_units,0)

    async def test_disconnect_after_dispatch_still_settles_once_without_redispatch(self):
        dispatched,finish=asyncio.Event(),asyncio.Event()
        calls=[]
        async def upstream(request):
            calls.append(request);dispatched.set();await finish.wait()
            return httpx.Response(200,json={'candidates':[{'finishReason':'STOP','content':{'parts':[{'text':'十七元'}]}}],
                'usageMetadata':{'promptTokenCount':35,'candidatesTokenCount':8,'totalTokenCount':43}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            relay=Relay(None,client=client)
            connection=ApplicationCommunicator(relay,{'type':'http','method':'POST','path':'/v1/media/observations',
                'headers':[(b'authorization',('Bearer '+self.key).encode()),(b'idempotency-key',b'media-observation:disconnect')]})
            await connection.send_input({'type':'http.request','body':json.dumps(self.payload).encode()})
            await asyncio.wait_for(dispatched.wait(),5)
            await connection.send_input({'type':'http.disconnect'})
            finish.set();await connection.wait(timeout=5)
            self.assertEqual((await self.call(relay,request_id='media-observation:disconnect'))[0],409)
        row=await db(Usage.objects.get,account=self.account)
        self.assertEqual((row.status,row.prompt_tokens,row.completion_tokens,row.reserved_units),('settled',35,8,0))
        self.assertEqual(len(calls),1)

    async def test_old_chat_json_and_sse_requests_keep_the_existing_protocol(self):
        calls=[]
        async def upstream(request):
            self.assertEqual(request.url.path,'/v1/chat/completions')
            payload=json.loads(request.content)
            self.assertNotIn('_native_media',payload)
            calls.append(payload)
            usage={'prompt_tokens':35,'completion_tokens':8,'total_tokens':43}
            if payload.get('stream'):
                return httpx.Response(200,content=('data: '+json.dumps({'choices':[{'index':0,'delta':{'content':'你好'},'finish_reason':'stop'}],
                    'usage':usage})+'\n\ndata: [DONE]\n\n').encode(),headers={'content-type':'text/event-stream'})
            return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':'你好'}}],'usage':usage})
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            relay=Relay(None,client=client)
            for model in ('qwen3.7-flash','gemini-3.8-flash','claude-sonnet-5-5'):
                for stream in (False,True):
                    connection=ApplicationCommunicator(relay,{'type':'http','method':'POST','path':'/v1/chat/completions',
                        'headers':[(b'authorization',('Bearer '+self.key).encode()),
                            (b'idempotency-key',('compat:'+model+':'+str(stream)).encode())]})
                    await connection.send_input({'type':'http.request','body':json.dumps({'model':model,'stream':stream,
                        'max_tokens':512,'messages':[{'role':'user','content':'synthetic old client input'}]}).encode()})
                    start=await connection.receive_output(timeout=5)
                    self.assertEqual(start['status'],200)
                    body=bytearray()
                    while True:
                        chunk=await connection.receive_output(timeout=5);body.extend(chunk['body'])
                        if not chunk.get('more_body'):break
                    await connection.wait(timeout=5)
                    if stream:self.assertIn(b'[DONE]',body)
                    else:self.assertEqual(json.loads(body)['choices'][0]['message']['content'],'你好')
        self.assertEqual(len(calls),6)
        self.assertEqual(await db(Usage.objects.filter(account=self.account,status='settled').count),6)
