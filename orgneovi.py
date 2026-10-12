# For NeoVim we have to delay the import because otherwise we would crash.
# So we try to import within the function after we have detected that neovintageous is installed.
import OrgEverywhere.packagecon as pkgcon
import logging

log = logging.getLogger(__name__)

r"""
/* SDOC: Reference::Commands::Misc
*** NeoVintageous clipboard
    When NeoVintageous is installed, the copy commands (Org Copy Subtree,
    Org Copy Entity, Org Copy Href...) also put the text in NeoVintageous's
    =0= register, so =p= pastes it. Without NeoVintageous nothing extra
    happens.
EDOC */
"""


def NeoVintageousYank(view, data: str):
	try:
		from NeoVintageous.nv.registers import _set
		_set(view, '0', [data], True)
		log.debug("NeoVintageous clipboard should now be set")
	except:
		print('Failed to copy neovintageous could not be referenced')
		pass

# NeoVintageous Clipboard is not the system clipboard.
# Which is annoying as hell, but sublime does not have
# the concept of registers... Sooo, we cheat and poke
# the NV registers if NV is present.
def TestAndSetClip(view, data: str):
	if(pkgcon.IsInstalled("NeoVintageous")):
		NeoVintageousYank(view, data)
	else:
		log.debug("No NeoVintageous - cannot set neovintageous clipboard")

