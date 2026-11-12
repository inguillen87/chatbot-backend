import React from 'react';
import {createRoot} from 'react-dom/client';
import {MemoryRouter} from 'react-router-dom';
import ChannelActivationChecklist from '@/components/profile/ChannelActivationChecklist';
import '@/index.css';

// Only the external identity exchange is a test boundary. This response and
// every guide/configuration response are emitted by the actual Flask application.
function App(){
  const [identity,setIdentity]=React.useState<number|null>(null);
  const [error,setError]=React.useState(false);
  React.useEffect(()=>{let current=true;void fetch('/auth/me',{credentials:'include',cache:'no-store'})
    .then(async response=>{if(!response.ok)throw new Error('fixture_identity_rejected');return response.json();})
    .then(value=>{const id=(value.user??value).id;if(!Number.isSafeInteger(id)||id<1)throw new Error('fixture_identity_invalid');if(current)setIdentity(id);})
    .catch(()=>{if(current)setError(true);});return()=>{current=false;};},[]);
  return <main style={{maxWidth:960,margin:'0 auto',padding:16}}>
    <h1>Evaluación integrada de guía</h1>
    {error?<p role="alert">Identidad de prueba no verificada</p>:null}
    {identity?<div data-testid="paired-identity" data-user-id={identity}>
      <ChannelActivationChecklist tenantSlug="acceptance-a" privateGuideSessionKey={'verified:'+identity} presentation="launch-journey"/>
    </div>:null}
  </main>;
}
createRoot(document.getElementById('root')!).render(<MemoryRouter><App/></MemoryRouter>);
