'use strict';
window.SabotageRoute=(()=>{
  const steps=[[-1,0],[0,1],[1,0],[0,-1]];
  function trace(board,directions){
    const path=[],seen=new Set(),size=board.size;
    const fail=reason=>({path,complete:false,reason});
    if(!Number.isInteger(size)||size<2||size>8||
       ![board.start,board.end].every(c=>Number.isInteger(c)&&c>=0&&c<size*size)||board.start===board.end||
       !Array.isArray(directions)||directions.length!==size*size||
       !directions.every(d=>Number.isInteger(d)&&d>=0&&d<4))return fail('invalid');
    let cell=board.start;
    while(!seen.has(cell)){
      seen.add(cell);path.push(cell);
      if(cell===board.end)return {path,complete:true,reason:null};
      const row=Math.floor(cell/size),col=cell%size,[dr,dc]=steps[directions[cell]],nr=row+dr,nc=col+dc;
      if(nr<0||nc<0||nr>=size||nc>=size)return fail('edge');
      if(Math.abs(nr-row)+Math.abs(nc-col)!==1)return fail('invalid');
      cell=nr*size+nc;
    }
    return fail('loop');
  }
  return {trace};
})();
