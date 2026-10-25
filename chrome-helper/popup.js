document.getElementById('resume').addEventListener('click', async () => {
 const button=document.getElementById('resume');button.disabled=true;
 try{const result=await chrome.runtime.sendMessage({type:'resume'});document.getElementById('status').textContent=result.message;}
 catch(error){document.getElementById('status').textContent=error.message;}
 finally{button.disabled=false;}
});

document.getElementById('release').addEventListener('click',async()=>{const result=await chrome.runtime.sendMessage({type:'release-reviewed'});document.getElementById('status').textContent=result.message;});
