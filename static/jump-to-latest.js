(function(){
  'use strict';

  // WHY: the existing End cue owns sticky-bottom and unread state; a second
  // arrow opening a 20-turn menu contradicted its label and duplicated navigation.
  var container=null;
  var btn=null;
  var observer=null;

  function syncVisibility(){
    if(btn) btn.hidden=!container || container.scrollHeight-container.scrollTop-container.clientHeight<150;
  }

  function jump(){
    // WHY: use the authoritative pin action, not scrollIntoView which leaves
    // live streaming unpinned and can move overflow-hidden ancestors.
    if(typeof window.scrollToBottom==='function') window.scrollToBottom();
    else if(container) container.scrollTop=container.scrollHeight;
    syncVisibility();
  }

  function ensure(){
    var next=document.getElementById('messages')||document.getElementById('msgInner');
    // WHY: normal pages already have End; the fallback is only for partial DOMs.
    if(document.getElementById('scrollToBottomBtn')) next=null;
    if(next!==container){
      if(container) container.removeEventListener('scroll',syncVisibility);
      if(observer) observer.disconnect();
      container=next;
      if(container){
        container.addEventListener('scroll',syncVisibility,{passive:true});
        // WHY: streamed text can grow without a scroll event while reading above.
        if(typeof MutationObserver==='function'){
          observer=new MutationObserver(syncVisibility);
          observer.observe(container,{childList:true,subtree:true,characterData:true});
        }
      }
    }
    if(container && !btn && document.body){
      btn=document.getElementById('jumpLatestBtn')||document.createElement('button');
      btn.id='jumpLatestBtn';
      btn.className='jump-latest-pill';
      btn.type='button';
      btn.textContent='↓ Latest';
      btn.setAttribute('aria-label','Jump to latest message');
      btn.hidden=true;
      btn.addEventListener('click',jump);
      document.body.appendChild(btn);
    }
    syncVisibility();
  }

  // WHY: repeated initialization must not add duplicate controls or listeners.
  if(window._jumpToLatest) return;
  window._jumpToLatest={ensure:ensure};
  window.addEventListener('resize',ensure);
  window.addEventListener('hashchange',ensure);
  window.addEventListener('popstate',ensure);
  if(document.readyState==='loading') document.addEventListener('DOMContentLoaded',ensure);
  else ensure();
})();
