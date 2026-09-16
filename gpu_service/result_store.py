"""Private R2 artifacts; only authenticated task queries receive short-lived URLs."""
import hashlib
import json
from pathlib import Path


class ResultStore:
    def __init__(self, config, client=None):
        self.bucket = config['bucket']
        self.prefix = config.get('prefix', 'olivia-gpu/results').strip('/')
        if client is None:
            import boto3
            from botocore.config import Config
            credentials = json.loads(Path(config['credentials_file']).read_text(encoding='utf-8'))
            client = boto3.client('s3', endpoint_url=config['endpoint'], region_name='auto',
                aws_access_key_id=credentials['access_key_id'], aws_secret_access_key=credentials['secret_access_key'],
                config=Config(signature_version='s3v4', connect_timeout=15, read_timeout=60,
                              retries={'max_attempts':3,'mode':'standard'}))
        self.client = client

    def publish(self, task_id, kind, job):
        source = job/'output.bin'
        extension = 'mp4' if kind in ('video','lipsync') else 'wav'
        key = f'{self.prefix}/{task_id}.{extension}'
        with source.open('rb') as stream:digest = hashlib.file_digest(stream,'sha256').hexdigest()
        self.client.upload_file(str(source), self.bucket, key, ExtraArgs={
            'Metadata':{'sha256':digest}, 'CacheControl':'private, no-store',
            'ContentType':'video/mp4' if extension=='mp4' else 'audio/wav',
            'ContentDisposition':f'attachment; filename="result.{extension}"'})
        head = self.client.head_object(Bucket=self.bucket, Key=key)
        if head['ContentLength'] != source.stat().st_size or head.get('Metadata',{}).get('sha256') != digest:
            raise ValueError('RESULT_UPLOAD_INVALID')
        receipt = job/'r2-result.json'
        temporary = receipt.with_suffix('.tmp')
        temporary.write_text(json.dumps({'bucket':self.bucket,'key':key,'sha256':digest}),encoding='utf-8')
        temporary.replace(receipt)

    def url(self, job):
        receipt = json.loads((job/'r2-result.json').read_text(encoding='utf-8'))
        if receipt['bucket'] != self.bucket or not receipt['key'].startswith(self.prefix+'/'):
            raise ValueError('RESULT_STORE_MISMATCH')
        return self.client.generate_presigned_url('get_object',
            Params={'Bucket':self.bucket,'Key':receipt['key']},ExpiresIn=900)
