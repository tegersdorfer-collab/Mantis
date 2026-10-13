"""Automatisch von Mantis erstelltes Skill: Startet den Schillfactory-Workflow und prüft die Erreichbarkeit der Postkristartenbank
Erstellt: 2026-07-05T21:59:48
"""
from core import tools as T


@T.register('check_schillfactory_workflow', 'Startet Schillfactory-Workflow und prüft Postkristartenbank-Erreichbarkeit', {}, [], 'system')
async def check_schillfactory_workflow():
    import httpx
    import json
    from datetime import datetime
    
    results = {
        'timestamp': datetime.now().isoformat(),
        'workflow': 'schillfactory',
        'checks': []
    }
    
    # Schillfactory-Workflow starten
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post('http://localhost:8080/schillfactory/start', json={'action': 'init'})
            results['checks'].append({
                'component': 'schillfactory_workflow',
                'status': 'started' if response.status_code == 200 else 'failed',
                'code': response.status_code
            })
    except Exception as e:
        results['checks'].append({
            'component': 'schillfactory_workflow',
            'status': 'error',
            'message': str(e)
        })
    
    # Postkristartenbank-Erreichbarkeit prüfen
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get('http://postkristartenbank.local/health')
            results['checks'].append({
                'component': 'postkristartenbank',
                'status': 'reachable' if response.status_code == 200 else 'unreachable',
                'code': response.status_code
            })
    except Exception as e:
        results['checks'].append({
            'component': 'postkristartenbank',
            'status': 'unreachable',
            'message': str(e)
        })
    
    return json.dumps(results, indent=2, ensure_ascii=False)
