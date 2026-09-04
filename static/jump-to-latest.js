(function(){
  'use strict';

  var container=null;
  var btn=null;
  var menu=null;
  var wiredContainer=null;
  var wiredBtn=null;
  var wiredInput=null;
  var wiredForm=null;
  var outsideClickAttached=false;
  var escapeAttached=false;
  var lifecycleWired=false;

  function resolveContainer(){
    try{
      return document.getElementById('messages')||document.getElementById('msgInner');
    }catch(_){
      return null;
    }
  }

  function isMenuOpen(){
    try{
      return !!(menu&&menu.style.display!=='none');
    }catch(_){
      return false;
    }
  }

  function removeOpenListeners(){
    try{
      if(outsideClickAttached) document.removeEventListener('click',handleDocumentClick);
    }catch(_){ }
    outsideClickAttached=false;
    try{
      if(escapeAttached) document.removeEventListener('keydown',handleDocumentKeydown);
    }catch(_){ }
    escapeAttached=false;
  }

  function closeMenu(){
    try{
      if(menu) menu.style.display='none';
    }catch(_){ }
    removeOpenListeners();
  }

  function handleDocumentClick(event){
    try{
      var target=event&&event.target;
      if((btn&&btn.contains(target))||(menu&&menu.contains(target))) return;
      closeMenu();
    }catch(_){
      closeMenu();
    }
  }

  function handleDocumentKeydown(event){
    try{
      if(event&&(event.key==='Escape'||event.keyCode===27)) closeMenu();
    }catch(_){
      closeMenu();
    }
  }

  function addOpenListeners(){
    removeOpenListeners();
    try{
      document.addEventListener('click',handleDocumentClick);
      outsideClickAttached=true;
    }catch(_){ }
    try{
      document.addEventListener('keydown',handleDocumentKeydown);
      escapeAttached=true;
    }catch(_){ }
  }

  function syncVisibility(){
    try{
      if(!container||!btn) return;
      btn.hidden=(container.scrollHeight-container.scrollTop-container.clientHeight<150);
    }catch(_){ }
  }

  function handleComposerFocus(){
    closeMenu();
  }

  function handleComposerSubmit(){
    closeMenu();
  }

  function handleButtonClick(){
    try{
      ensure();
      if(!container) return;
      if(isMenuOpen()){
        closeMenu();
        return;
      }
      var rows=Array.from(container.querySelectorAll('.msg-row[data-role="user"]'));
      if(rows.length){
        openMenu();
        return;
      }
      if(typeof container.scrollTo==='function'){
        container.scrollTo({top:container.scrollHeight,behavior:'smooth'});
      }
    }catch(_){ }
  }

  function ensure(){
    try{
      var nextContainer=resolveContainer();
      if(!nextContainer) return;
      container=nextContainer;

      var nextBtn=document.getElementById('jumpLatestBtn');
      if(!nextBtn){
        nextBtn=document.createElement('button');
        nextBtn.id='jumpLatestBtn';
        nextBtn.className='jump-latest-pill';
        nextBtn.type='button';
        nextBtn.textContent='↓';
        nextBtn.setAttribute('aria-label','Scroll to bottom');
        nextBtn.hidden=true;
        document.body.appendChild(nextBtn);
      }
      btn=nextBtn;

      var nextMenu=document.getElementById('jumpLatestMenu');
      if(!nextMenu){
        nextMenu=document.createElement('div');
        nextMenu.id='jumpLatestMenu';
        nextMenu.className='jump-latest-menu';
        nextMenu.setAttribute('role','listbox');
        nextMenu.setAttribute('aria-label','Recent messages');
        nextMenu.style.display='none';
        document.body.appendChild(nextMenu);
      }
      menu=nextMenu;

      if(wiredContainer!==container){
        if(wiredContainer) wiredContainer.removeEventListener('scroll',syncVisibility);
        container.addEventListener('scroll',syncVisibility);
        wiredContainer=container;
      }
      if(wiredBtn!==btn){
        if(wiredBtn) wiredBtn.removeEventListener('click',handleButtonClick);
        btn.addEventListener('click',handleButtonClick);
        wiredBtn=btn;
      }

      var nextInput=document.getElementById('messagesInput')||document.getElementById('msg');
      if(wiredInput!==nextInput){
        if(wiredInput) wiredInput.removeEventListener('focus',handleComposerFocus);
        if(nextInput) nextInput.addEventListener('focus',handleComposerFocus);
        wiredInput=nextInput;
      }

      var nextForm=nextInput&&nextInput.form?nextInput.form:null;
      if(!nextForm) nextForm=document.getElementById('composerForm');
      if(!nextForm) nextForm=document.querySelector('#composerWrap form');
      if(wiredForm!==nextForm){
        if(wiredForm) wiredForm.removeEventListener('submit',handleComposerSubmit);
        if(nextForm) nextForm.addEventListener('submit',handleComposerSubmit);
        wiredForm=nextForm;
      }

      if(!lifecycleWired){
        window.addEventListener('hashchange',closeMenu);
        window.addEventListener('popstate',closeMenu);
        lifecycleWired=true;
      }

      syncVisibility();
    }catch(_){ }
  }

  function formatRowTime(row){
    try{
      var raw=row.getAttribute('data-ts');
      if(raw===null) return null;
      var epoch=Number(raw);
      if(!isFinite(epoch)) return null;
      var date=new Date(epoch*1000);
      if(isNaN(date.getTime())) return null;
      var hours=String(date.getHours()).padStart(2,'0');
      var minutes=String(date.getMinutes()).padStart(2,'0');
      return hours+':'+minutes;
    }catch(_){
      return null;
    }
  }

  function buildList(){
    try{
      ensure();
      if(!container||!menu) return 0;
      var rows=Array.from(container.querySelectorAll('.msg-row[data-role="user"]'));
      menu.innerHTML='';
      if(!rows.length){
        closeMenu();
        return 0;
      }

      var fragment=document.createDocumentFragment();
      var items=[];
      rows.slice(-20).forEach(function(row){
        var firstLine=String(row.innerText||'').split(/\r?\n/,1)[0].replace(/\s+/g,' ').trim();
        if(!firstLine) return;
        var title=firstLine.length>72?firstLine.slice(0,72)+'…':firstLine;
        var item=document.createElement('button');
        item.className='jump-latest-item';
        item.type='button';
        item.setAttribute('role','option');

        var titleSpan=document.createElement('span');
        titleSpan.className='jump-latest-title';
        titleSpan.textContent=title;
        item.appendChild(titleSpan);

        var time=formatRowTime(row);
        if(time!==null){
          var timeSpan=document.createElement('span');
          timeSpan.className='jump-latest-time';
          timeSpan.textContent=time;
          item.appendChild(timeSpan);
        }

        item.addEventListener('click',function(){
          jumpToRow(row);
        });
        fragment.appendChild(item);
        items.push(item);
      });

      if(!items.length){
        closeMenu();
        return 0;
      }
      items[items.length-1].classList.add('is-latest');
      menu.appendChild(fragment);
      return items.length;
    }catch(_){
      closeMenu();
      return 0;
    }
  }

  function openMenu(){
    try{
      ensure();
      if(!menu||!buildList()) return;
      menu.style.display='block';
      addOpenListeners();
    }catch(_){
      closeMenu();
    }
  }

  function jumpToRow(row){
    try{
      if(!row||typeof row.scrollIntoView!=='function'){
        closeMenu();
        return;
      }
      if(row._jumpLatestFlashTimer) clearTimeout(row._jumpLatestFlashTimer);
      row.classList.remove('jump-flash');
      row.scrollIntoView({behavior:'smooth',block:'start'});
      row.classList.add('jump-flash');
      row._jumpLatestFlashTimer=setTimeout(function(){
        try{
          row.classList.remove('jump-flash');
          row._jumpLatestFlashTimer=null;
        }catch(_){ }
      },1200);
      closeMenu();
    }catch(_){
      closeMenu();
    }
  }

  window._jumpToLatest={
    ensure:ensure,
    openMenu:openMenu,
    closeMenu:closeMenu,
    buildList:buildList,
    jumpToRow:jumpToRow
  };

  try{
    if(document.readyState==='loading') document.addEventListener('DOMContentLoaded',ensure);
    else ensure();
  }catch(_){ }
})();
