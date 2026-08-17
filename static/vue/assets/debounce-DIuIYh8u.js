function c(t,n=300){let e=null;const l=(...u)=>{clearTimeout(e),e=setTimeout(()=>{e=null,t(...u)},n)};return l.cancel=()=>{e&&clearTimeout(e),e=null},l}export{c as d};
