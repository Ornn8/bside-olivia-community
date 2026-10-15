"""Story reading and relationship details in the existing diary surface."""
JAVASCRIPT = r'''(() => {
  window.oliviaStoryView = async (main, {text, button, request}) => {
    const token=main.oliviaDiaryViewToken={};
    main.replaceChildren(text('p', '正在翻开我们的故事……'));
    const load = () => window.oliviaStoryView(main, {text, button, request});
    try {
      const data = await request('/toy/story');
      if (!main.isConnected || main.oliviaDiaryViewToken!==token) return;
      const intro = text('p', '她把你们的关系写成一部小说，关系往前走会开新卷，新章节跟着你们的经历来写。');
      const price = text('p', `每章按故事模型的价格计费，与日记一样包含写作和润色。当前模型：${data.model}。`);
      const status = text('p', {writing:'她正在写这一章，稍后可以刷新查看。',deferred:'余额不足，充值后会继续写',failed:'这一章暂时没有写完，稍后会继续尝试。',written:'新的篇章已经写好。'}[data.status] || '');
      status.setAttribute('role', 'status');
      const toggle = button(data.enabled ? '暂停写故事' : '开始写我们的故事', async () => {
        toggle.disabled = true;
        try { await request('/toy/story/settings', {enabled:!data.enabled}, {confirmed:true}); await load(); }
        catch (_) { status.textContent='没有保存成功，请稍后重试。'; toggle.disabled=false; }
      });
      main.replaceChildren(intro, price, toggle, status, button('刷新章节', load));
      const read = async chapter => {
        let seenFailed=false;
        try { await request('/toy/story/seen', {id:chapter.id}, {confirmed:true}); } catch (_) { seenFailed=true; }
        if(main.oliviaDiaryViewToken!==token)return;
        const article = document.createElement('article'); article.className='od-entry';
        article.style.cssText='max-width:70ch;margin:24px auto;line-height:1.9;overflow-wrap:anywhere';
        article.append(text('h2', chapter.title));
        for (const paragraph of chapter.body.split('\n').filter(Boolean)) article.append(text('p', paragraph));
        const notice = text('p', ''); notice.setAttribute('role', 'status');
        if(seenFailed)notice.textContent='本章可以阅读，但已读标记没有保存，请稍后重新打开。';
        const rewrite = button('重写本章', () => {
          notice.replaceChildren(text('span', `重写将按 ${data.model} 的价格计费，完成后替换本章。`),
            button('确认付费重写', async () => {
              try { await request('/toy/story/rewrite', {id:chapter.id}, {confirmed:true}); notice.textContent='正在重写，稍后刷新章节查看。'; }
              catch (_) { notice.textContent='没有开始重写，请稍后重试。'; }
            }), button('取消', () => notice.replaceChildren()));
        });
        const remove = button('删除本章', () => notice.replaceChildren(text('span', '删除后无法恢复。'),
          button('确认删除', async () => {
            try { await request('/toy/story/delete', {id:chapter.id}, {confirmed:true}); await load(); }
            catch (_) { notice.textContent='本章没有删除成功，请稍后重试。'; }
          }),
          button('取消', () => notice.replaceChildren())));
        const commentLabel=text('label','本章留言（最多200字）');
        const comment=document.createElement('textarea');comment.maxLength=200;comment.value=chapter.comment||'';
        comment.style.cssText='display:block;width:100%;max-width:70ch;box-sizing:border-box;min-height:96px;background:#202225;color:#eee;border:1px solid #777;border-radius:8px;padding:12px;font:inherit';
        commentLabel.append(comment);
        const saveComment=async value=>{
          try { await request('/toy/story/comment',{id:chapter.id,text:value},{confirmed:true});chapter.comment=value;comment.value=value;notice.textContent=value?'留言已保存，会作为下一章的参考。':'留言已删除。'; }
          catch(_){notice.textContent='留言没有保存成功，请稍后重试。';}
        };
        main.replaceChildren(button('返回目录', load), article, commentLabel,
          button('保存留言',()=>saveComment(comment.value)),button('删除留言',()=>saveComment('')),rewrite, remove, notice);
      };
      for (const volume of data.volumes) {
        const section = document.createElement('section'); section.style.marginTop='28px';
        section.append(text('h2', `第 ${volume.volume} 卷 · ${volume.title}`), text('p', volume.description));
        for (const chapter of volume.chapters) section.append(button(`${chapter.seen?'':'未读 · '}${chapter.title}`, () => read(chapter)));
        if (!volume.chapters.length) section.append(text('p', data.enabled ? '这一卷正在等待新的篇章。' : '开启后，她会从序章写起。'));
        main.append(section);
      }
    } catch (_) { if(main.oliviaDiaryViewToken===token)main.replaceChildren(text('p', '故事暂时打不开，请稍后重试。'), button('重试', load)); }
  };
  window.oliviaRelationshipView = (content, data, {text, button, request, refresh}) => {
    content.setAttribute('data-relationship-story','');
    const style=document.createElement('style');
    style.textContent='[data-relationship-story] button,[data-relationship-story] select{min-height:44px;max-width:100%;box-sizing:border-box;justify-self:start;background:#292b2e;color:#ded9d1;border:1px solid #686a70;border-radius:12px;padding:8px 16px;font:inherit}[data-relationship-story] button:focus-visible,[data-relationship-story] select:focus-visible{outline:2px solid #ded9d1;outline-offset:3px}';
    const labels = {familiarity:'熟悉', trust:'信任', comfort:'安心', closeness:'亲近', tension:'紧张'};
    const select = document.createElement('select'); select.setAttribute('aria-label', '相处方式');
    for (const [value, label] of Object.entries(data.routes)) {
      const option = document.createElement('option'); option.value=value; option.textContent=label; select.append(option);
    }
    select.value=data.route;
    const status=text('p',''); status.setAttribute('role','status');
    const save=button('保存相处方式',async()=>{
      save.disabled=true;
      try { await request('/toy/relationship-story/route',{route:select.value,version:data.version},{confirmed:true}); await refresh(); }
      catch (_) { status.textContent='没有保存成功，关系可能已经更新，请重新打开后再试。'; save.disabled=false; }
    });
    const controls=document.createElement('div');controls.style.cssText='display:flex;flex-wrap:wrap;gap:12px';controls.append(select,save);
    content.replaceChildren(style,text('h3','相处方式'),controls,status,
      text('h3',`第 ${data.volume} 卷 · ${data.title}`),text('p',data.description));
    for(const [key, value] of Object.entries(data.dimensions)) {
      content.append(text('p',`${labels[key]}：${value.text}${value.rising?' '+(value.rising_text||'最近有所上升。'):''}`));
    }
    content.append(text('h3','称呼'),text('p',`她怎么叫你：${data.addressing.linli_calls_user||'还没有固定的称呼'}`),
      text('p',`你怎么叫她：${data.addressing.user_calls_linli||'还没有固定的称呼'}`));
    for(const [title, items] of [['最近的关系时刻',data.moments],['准备写进故事的时刻',data.pending_moments||[]],['进行中的约定',data.plans]]) {
      content.append(text('h3',title));
      if(!items.length)content.append(text('p','还没有记录。'));
      for(const item of items){
        const row=document.createElement('div');
        row.append(text('p',`${item.date} · ${item.text}${item.quote?' — '+item.quote:''}`));
        if(item.id){
          const mark=button(item.marked?'取消写进故事':'写进故事',async()=>{
            mark.disabled=true;
            try{await request('/toy/relationship-story/mark',{id:item.id,enabled:!item.marked},{confirmed:true});await refresh();}
            catch(_){status.textContent='没有保存成功，最多可选3个待写时刻；请刷新后重试。';mark.disabled=false;}
          });row.append(mark);
        }
        content.append(row);
      }
    }
    content.append(button('读我们的故事',()=>{window.location.hash='#/world/diary?story=1';}));
  };
})();'''
