"""Layout-only fixture: retain one native source; never invent WB proof."""
from apps.search_cluster_cleaner_web_fixture import OWNER
from packages.adapters.search_cluster_cleaner_http import PREFIX

def retained_fixture_outcome(f,route,outcome):
    """Synthetic admitted-consumer outcome; actual native immutable source TX.

    Layout fixtures below simulate worker/WB results. They do not prove WB
    completion. The shared receipt reads the real source and remains attention
    or processing when exact native events/items are absent. Actual full native
    command/readback coverage lives in operator_cleaner_operations_smoke.py.
    """
    from packages.application.operator_cleaner_operations import receipt
    from urllib.parse import urlparse
    payload=route.request.post_data_json
    path=urlparse(route.request.url).path[len(PREFIX)+1:]
    # Separate synthetic layout-consumer records from actual cleaner routes:
    # summary must not treat fake job IDs as production native job bindings.
    native=f.cleaner._command(OWNER,'ui-fixture:'+path,payload,lambda conn,actor:outcome)
    return dict(native,acceptance=receipt(f.web,OWNER,payload['request_id']))
