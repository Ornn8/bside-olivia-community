"""Expose native playback modes and keep library playback in the library."""


def patch_music_playback(source: str) -> str:
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
