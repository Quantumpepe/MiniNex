import React, {useState} from 'react';
import {Info, X} from 'lucide-react';

// Short, touch-friendly in-app explanations; content follows selected DE/EN dictionary.
export default function InfoTip({title,children,label='Info'}) {
  const [open,setOpen]=useState(false);
  return <span style={{display:'inline-flex',position:'relative',alignItems:'center',verticalAlign:'middle',marginLeft:8}}>
    <button type="button" aria-label={label+': '+title} aria-expanded={open}
      onClick={()=>setOpen(v=>!v)}
      style={{display:'inline-flex',alignItems:'center',justifyContent:'center',width:30,height:30,borderRadius:'50%',border:'1px solid currentColor',background:'transparent',color:'inherit',cursor:'pointer'}}>
      {open?<X size={17}/>:<Info size={17}/>}
    </button>
    {open&&<span role="status" style={{position:'absolute',right:0,top:34,zIndex:100,display:'block',width:'min(300px, 80vw)',padding:14,borderRadius:12,background:'#102a2a',color:'#f2faf7',border:'1px solid #35645a',boxShadow:'0 10px 24px #0008',textAlign:'left',whiteSpace:'normal',fontSize:13,lineHeight:1.55}}>
      <strong style={{display:'block',marginBottom:7}}>{title}</strong>{children}
    </span>}
  </span>;
}
