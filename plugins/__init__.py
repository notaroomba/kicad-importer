try:
    import pcbnew
except ImportError:  # imported outside KiCad, e.g. tests
    pcbnew = None

if pcbnew:
    from .importer import ImporterPlugin

    ImporterPlugin().register()
