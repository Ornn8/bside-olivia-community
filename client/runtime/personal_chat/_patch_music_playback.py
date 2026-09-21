"""Expose native playback modes and keep library playback in the library."""


def patch_music_playback(source: str) -> str:
    source = _patch_native_mode_button(source)
    if 'window.__oliviaMusicPlayback=' in source:
        return source
    anchors = {
        'p.value===ot.Shuffle?p.value=ot.Repeat:p.value===ot.Repeat?p.value=ot.Single:p.value===ot.Single&&(p.value=ot.Shuffle,u.value?I.value=[u.value.itemId]:I.value=[])':
            'p.value=p.value===ot.Shuffle?ot.Repeat:ot.Shuffle,I.value=u.value?[u.value.itemId]:[]',
        'if(h.value==="songlist"){const K=x.value.findIndex(W=>a(W));K!==-1&&M(x.value[K]);return}':
            'if(h.value==="songlist"){window.__oliviaMusicPlayback.next(1);return}',
        'if(h.value==="songlist")return;':
            'if(h.value==="songlist"){window.__oliviaMusicPlayback.next(-1);return}',
        'h.value==="songlist"){const K=x.value.findIndex(W=>a(W));K!==-1?M(x.value[K]):(f.value=null,d.value=0)}':
            'h.value==="songlist"){window.__oliviaMusicPlayback.next(1)}',
        'return{isSongAvailable:a,isPlaying:ao(m)': '''
window.__oliviaMusicPlayback={
  get mode(){return p.value},
  setMode(mode){if((mode==="repeat"||mode==="shuffle")&&mode!==p.value)ne()},
  next(direction){
    const songs=(window.__oliviaLocalSongCatalog?.songs.value||[])
      .filter(song=>t.value!==Se.LITE||i.isDownloaded(song.id));
    if(!songs.length){f.value=null;d.value=0;return}
    const current=songs.findIndex(song=>song.id===f.value?.id);
    let index;
    if(p.value===ot.Shuffle&&songs.length>1){
      const candidates=songs.filter(song=>song.id!==f.value?.id);
      A(candidates[Math.floor(Math.random()*candidates.length)]);return;
    }
    index=current<0?0:(current+direction+songs.length)%songs.length;
    A(songs[index]);
  }
};
_e(p,()=>window.dispatchEvent(new Event("olivia-music-mode-changed")));
window.dispatchEvent(new Event("olivia-music-mode-changed"));
return{isSongAvailable:a,isPlaying:ao(m)''',
    }
    # Do not partially patch a different native client version.
    if not all(anchor in source for anchor in anchors):
        return source
    for anchor, replacement in anchors.items():
        source = source.replace(anchor, replacement, 1)
    return source


def _patch_native_mode_button(source: str) -> str:
    # The native studio toolbar hides mode switching in offline mode. Reuse
    # its existing handler/icons beside Play, including on already-patched apps.
    before = 'o(t)?Y("",!0):(r(),_("div",{key:0,class:"w-8 h-8 flex items-center justify-center cursor-pointer hover:bg-grey-1 rounded-1",onClick:g},['
    after = '(r(),_("button",{key:0,type:"button",title:o(l)===o(ot).Shuffle?"随机播放，点击切换为顺序播放":"顺序播放，点击切换为随机播放","aria-label":o(l)===o(ot).Shuffle?"随机播放":"顺序播放",class:"w-8 h-8 flex items-center justify-center cursor-pointer hover:bg-grey-1 rounded-1",onClick:g},['
    end = 'type:"repeatsingle",class:"text-headline-m text-info hover:text-info-hover active:text-info-active"})):Y("",!0)])),o(t)?Y("",!0):(r(),_("div",{key:1'
    if source.count(before) == 1 and source.count(end) == 1:
        source = source.replace(before, after, 1).replace(
            end, end.replace('Y("",!0)]))', 'Y("",!0)],8,["title","aria-label"]))'), 1)
    return source
