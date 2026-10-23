"""Infrastructure smoke test: QGIS app, fake server, plugin import."""

from helpers import build_network


def test_qgis_and_server_up(authed_api, loc_server):
    data = authed_api.get_all_locations()
    assert len(data) == 2
    layers = build_network()
    assert layers["poles"].featureCount() == 5


def test_classfactory(iface):
    import label_on_a_cable
    plugin = label_on_a_cable.classFactory(iface)
    plugin.initGui()
    assert set(plugin.actions) == {"login", "workspace", "mapping",
                                   "generate", "push", "pull"}
    plugin.unload()
