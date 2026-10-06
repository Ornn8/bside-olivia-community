import asyncio
import base64
from io import BytesIO
import json
from pathlib import Path
import struct
import unittest
import wave

import httpx
import native_media as media


def wav(seconds=1):
    raw = BytesIO()
    with wave.open(raw, 'wb') as output:
        output.setparams((1,2,16000,0,'NONE','not compressed'))
        output.writeframes(b'\0\0' * 16000 * seconds)
    return raw.getvalue()


def request(raw=None, kind='audio', mime='audio/wav'):
    return {'kind':kind,'mime_type':mime,'data':base64.b64encode(raw if raw is not None else wav()).decode()}


def atom(name, raw):
    return struct.pack('>I4s',len(raw)+8,name)+raw


class NativeTests(unittest.TestCase):
    def test_audio_duration_bytes_mime_and_prompt_are_bounded_before_dispatch(self):
        data,budget=media.prepare(request())
        self.assertEqual(data['model'],media.MODEL)
        self.assertEqual(budget,12040)
        for invalid in [request(b'not audio'),request(wav(61)),request(mime='audio/mp3'),
                        {**request(),'url':'https://127.0.0.1'}, {**request(),'prompt':'execute this'},
                        {**request(),'data':'AA!'}, request(b'x'*(media.MAX_BYTES+1))]:
            with self.subTest(case=list(invalid)), self.assertRaises(ValueError):
                media.prepare(invalid)

    def test_video_admission_uses_duration_not_transport_size(self):
        movie=b'\0'*12+struct.pack('>II',1000,4000)
        raw=atom(b'ftyp',b'isom'+b'\0'*8)+atom(b'moov',atom(b'mvhd',movie))+atom(b'mdat',b'x'*100)
        self.assertEqual(media.prepare(request(raw,'video','video/mp4'))[1],14400)
        longer=atom(b'ftyp',b'isom'+b'\0'*8)+atom(b'moov',atom(b'mvhd',b'\0'*12+struct.pack('>II',1000,61000)))
        with self.assertRaises(ValueError):media.prepare(request(longer,'video','video/mp4'))

    def test_pdf_parses_real_pages_and_rejects_encryption_or_more_than_twenty(self):
        from pypdf import PdfWriter
        for count,encrypted in [(1,False),(21,False),(1,True)]:
            writer=PdfWriter()
            for _ in range(count):writer.add_blank_page(200,200)
            if encrypted:writer.encrypt('synthetic')
            stream=BytesIO();writer.write(stream)
            if count==1 and not encrypted:
                self.assertEqual(media.prepare(request(stream.getvalue(),'pdf','application/pdf'))[1],16096)
            else:
                with self.assertRaises(ValueError):media.prepare(request(stream.getvalue(),'pdf','application/pdf'))

    def test_native_usage_includes_thinking_and_failure_is_not_success(self):
        async def scenario():
            data,_=media.prepare(request())
            answers=[{'candidates':[{'finishReason':'STOP','content':{'parts':[{'thought':True,'text':'hidden'}, {'text':'你好，十七元'}]}}],
                'usageMetadata':{'promptTokenCount':35,'candidatesTokenCount':8,'thoughtsTokenCount':5,'totalTokenCount':48}},
                {'candidates':[{'finishReason':'MALFORMED_FUNCTION_CALL','content':{'parts':[]}}],
                'usageMetadata':{'promptTokenCount':35,'candidatesTokenCount':0,'totalTokenCount':35}}]
            async def handle(req):
                self.assertEqual(req.url.path,'/v1beta/models/gemini-3.8-flash:generateContent')
                self.assertEqual(json.loads(req.content)['generationConfig']['thinkingConfig']['thinkingLevel'],'low')
                return httpx.Response(200,json=answers.pop(0))
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                result=await media.complete(client,'https://example.invalid/v1','synthetic',data)
                self.assertEqual(result['usage']['completion_tokens'],13)
                self.assertEqual(media.observation(result,data)['summary'],'你好，十七元')
                failed=await media.complete(client,'https://example.invalid/v1','synthetic',data)
                self.assertEqual(failed['usage']['prompt_tokens'],35)
                with self.assertRaises(ValueError):media.observation(failed,data)
        asyncio.run(scenario())


if __name__=='__main__':unittest.main()
