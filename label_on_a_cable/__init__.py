"""Label on a Cable - KelTech IoE QGIS Plugin.

LOC QGIS plugin for automated generation, synchronisation, and management
of infrastructure labels.
"""


def classFactory(iface):
    """QGIS plugin entrypoint.

    Called by QGIS on plugin load. Must return an object with
    initGui() and unload() methods.
    """
    from .plugin import LabelOnACablePlugin
    return LabelOnACablePlugin(iface)
