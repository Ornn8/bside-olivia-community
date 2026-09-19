// Render persisted backend-validated metadata; old letters receive a base illustration.
export function oliviaLetterSticker(value) {
 return typeof value==='string'&&/^linli-(0[1-9]|[1-9][0-9]|10[0-8])$/.test(value)?value:'linli-01';
}

if(typeof document!=='undefined'&&!document.getElementById('olivia-letter-sticker-style')) {
 const style=document.createElement('style');style.id='olivia-letter-sticker-style';
 style.textContent=`
 .mail-box-reply-content-text .olivia-letter-sticker{position:absolute;left:4.65%;bottom:3cqw;width:14%;height:auto;aspect-ratio:1;object-fit:contain;pointer-events:none}
 .mail-box-reply-content-text .olivia-letter-signature{position:absolute;right:6%;bottom:max(42px,9cqw);max-width:60%;text-align:right;white-space:pre-wrap;overflow-wrap:anywhere;font-size:16px;color:var(--tp-grey-0);line-height:1.6}
 .mail-box-reply-content-text .mail-box-reply-content-textarea{flex:1;min-height:0;height:0;width:calc(100% - 48px);margin:20px 24px max(78px,19cqw);line-height:1.6;white-space:pre-wrap}
 .mail-box-reply-content-text:has(olivia-letter-audio) .mail-box-reply-content-textarea{flex:none;height:180px}
 .mail-box-reply-content-text:has(olivia-letter-audio){aspect-ratio:auto!important;height:auto!important}
 `;
 document.head.appendChild(style);
}
