# Amazon Prime UK

Der Provider `amazon_prime_uk` verwendet denselben Prime-Video-Detailseiten-Adapter wie
`amazon_prime_de`. Er ruft die öffentliche, nicht dokumentierte Detailseiten-API mit der
UK-Marketplace-URL auf und normalisiert Staffeln, Episoden und Veröffentlichungsdaten in
das gemeinsame Datenmodell.

Die konfigurierte Serien-ID ist die Prime-Video-Titel-ID. Für den ersten Test ist
`0JAJ59KRPSJY5QIDTK0WTBWMR6` für `LOL: Last One Laughing UK` hinterlegt.

Die API ist nicht als öffentliche Consumer-API dokumentiert und kann sich jederzeit ändern.
